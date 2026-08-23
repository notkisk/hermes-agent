"""Cross-process cache for resolved tool definitions.

``model_tools.get_tool_definitions()`` is deterministic given (a) the code
that registers tools, (b) configuration, and (c) credential-shaped env
vars — but computing it imports every ``tools/*.py`` module (~225 ms of
fan-out), runs ~30 check_fns, sanitizes schemas, and assembles the
tool-search tier. The interactive banner pays all of it before the prompt
can render.

This module lets the banner consult a disk cache BEFORE importing
``model_tools`` at all. The cache key is a fingerprint over exactly the
inputs above, so any relevant change recomputes. When MCP servers are
configured the cache is bypassed entirely: discovered MCP tools are not
covered by the fingerprint.

Secret values are never written — only name + digest pairs inside the
fingerprint.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_TOOL_DEFS_CACHE_VERSION = 1


def _cache_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "cache" / "tool_definitions"


def _code_fingerprint() -> str:
    """Hash mtimes+sizes of everything that feeds tool registration."""
    h = hashlib.sha256()

    def _feed(*parts: Any) -> None:
        h.update(("|".join(str(p) for p in parts) + "\n").encode("utf-8", "replace"))

    roots = []
    try:
        import hermes_constants
        root = Path(__import__("hermes_cli.config", fromlist=["get_project_root"]).get_project_root())
        roots.append(root)
    except Exception:
        pass
    if not roots:
        return "unavailable"

    patterns = [
        "tools/*.py",
        "tools/environments/*.py",
        "tools/computer_use/**/*.py",
        "model_tools.py",
        "toolsets.py",
        "toolset_distributions.py",
        "plugins/*/plugin.yaml",
        "plugins/*/*/plugin.yaml",
    ]
    for pattern in patterns:
        for path in sorted(glob.glob(str(roots[0] / pattern), recursive=True)):
            try:
                st = os.stat(path)
                _feed(path, st.st_mtime_ns, st.st_size)
            except OSError:
                continue

    # User-installed plugins can register tools too.
    try:
        from hermes_constants import get_hermes_home
        user_plugins = get_hermes_home() / "plugins"
        if user_plugins.is_dir():
            for path in sorted(user_plugins.rglob("*.py"))[:500]:
                try:
                    st = os.stat(path)
                    _feed(path, st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
            for path in sorted(user_plugins.rglob("plugin.yaml"))[:200]:
                try:
                    st = os.stat(path)
                    _feed(path, st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
    except Exception:
        pass

    try:
        from hermes_cli import __version__ as _v
        _feed("version", _v)
    except Exception:
        pass
    return h.hexdigest()


def _config_fingerprint() -> str:
    """Hash config identity + credential-shaped env names/value digests."""
    h = hashlib.sha256()

    def _feed(*parts: Any) -> None:
        h.update(("|".join(str(p) for p in parts) + "\n").encode("utf-8", "replace"))

    try:
        from hermes_constants import get_hermes_home
        for rel in ("config.yaml", "auth.json"):
            try:
                st = (get_hermes_home() / rel).stat()
                _feed(rel, st.st_mtime_ns, st.st_size)
            except OSError:
                _feed(rel, "missing")
    except Exception:
        pass

    try:
        from hermes_cli.config import read_raw_config
        raw = read_raw_config() or {}
        tools_cfg = {
            k: v for k, v in raw.items()
            if k.split(".")[0] in ("tools", "toolsets", "security", "mcp_servers")
        }
        _feed("cfg", json.dumps(tools_cfg, sort_keys=True, default=str))
    except Exception as e:
        logger.debug("tool-defs cache config read failed: %s", e)

    # Credential-shaped env vars: name + digest of the value. Session-scoped
    # HERMES_* vars (session ids, RPC sockets, kanban run ids...) are
    # deliberately EXCLUDED — they change per launch and none of them feed
    # tool registration; including them would make the key unique per
    # session and the cache permanently cold.
    for name in sorted(os.environ):
        if name.endswith("_API_KEY") or name.endswith("_TOKEN"):
            val = os.environ.get(name) or ""
            _feed(name, hashlib.sha256(val.encode("utf-8")).hexdigest())
    for name in ("HERMES_SAFE_MODE", "HERMES_PLATFORM", "HERMES_PROFILE",
                 "HERMES_IGNORE_USER_CONFIG"):
        val = os.environ.get(name)
        if val:
            _feed(name, val)
    return h.hexdigest()


def cache_key(
    enabled_toolsets: Optional[List[str]] = None,
    disabled_toolsets: Optional[List[str]] = None,
    platform: str = "",
    api_mode: str = "",
    skip_tool_search_assembly: bool = False,
) -> Tuple[str, str]:
    """Return ``(key, fingerprint)`` for these get_tool_definitions params."""
    fp = _code_fingerprint() + "\n" + _config_fingerprint()
    params = {
        "enabled": sorted(enabled_toolsets or []),
        "disabled": sorted(disabled_toolsets or []),
        "platform": platform,
        "api_mode": api_mode,
        "skip_ts": bool(skip_tool_search_assembly),
        "v": _TOOL_DEFS_CACHE_VERSION,
    }
    blob = fp + "\n" + json.dumps(params, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest(), fp


def load_cached(key: str) -> Optional[List[Dict[str, Any]]]:
    """Return cached tool defs for ``key``, else None."""
    path = _cache_dir() / f"{key}.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return None
    if not isinstance(blob, dict) or blob.get("v") != _TOOL_DEFS_CACHE_VERSION:
        return None
    defs = blob.get("defs")
    if not isinstance(defs, list) or not defs:
        return None
    return defs


def store(key: str, defs: List[Dict[str, Any]]) -> None:
    """Persist tool defs atomically; prune old entries; failures are non-fatal."""
    if not defs:
        return
    try:
        d = _cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".{key}.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"v": _TOOL_DEFS_CACHE_VERSION, "defs": defs,
                       "ts": time.time()}, f)
        os.replace(tmp, d / f"{key}.json")
        # keep the newest few entries; keys are content-addressed so old ones
        # only linger after config/code changes
        entries = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime_ns,
                         reverse=True)
        for stale in entries[6:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception as e:
        logger.debug("tool-defs cache write failed: %s", e)


def should_bypass_for_mcp() -> bool:
    """True when configured MCP servers make cached defs unsafe to serve."""
    try:
        from hermes_cli.mcp_startup import _has_configured_mcp_servers
        return _has_configured_mcp_servers()
    except Exception:
        return True  # fail open: recompute rather than serve blind
