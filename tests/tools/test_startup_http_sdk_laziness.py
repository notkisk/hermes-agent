"""Startup guard: heavy HTTP SDKs must stay off the tool-discovery path.

Importing ``model_tools`` triggers ``discover_builtin_tools()``, which
imports every ``tools/*.py`` with a registered tool, plus the general
PluginManager's bundled plugins. Several of those modules used to pull the
``httpx`` / ``requests`` SDK trees (~55 ms each) at module level even though
the SDKs are only needed when their network calls actually run. They are
loaded lazily now; these tests pin that so a future eager re-import fails
loudly instead of silently re-costing every CLI/gateway/TUI session start.
"""

import subprocess
import sys


_SNIPPET_HEADER = (
    "import sys\n"
    "sys.path.insert(0, '.')\n"
)


def _run_snippet(snippet: str) -> list:
    """Run code in a fresh interpreter; return the printed LOADED list."""
    out = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    for line in out.stdout.splitlines():
        if line.startswith("LOADED:"):
            return eval(line.split(":", 1)[1])
    raise AssertionError(f"no LOADED marker in output: {out.stdout!r}")


class TestHttpSdkLaziness:
    def test_model_tools_discovery_loads_no_http_sdk(self):
        """Tool discovery + plugin load must not drag in httpx/requests/openai."""
        snippet = (
            _SNIPPET_HEADER
            + "import model_tools\n"
            + "heavy = [m for m in ('httpx', 'requests', 'openai') if m in sys.modules]\n"
            + "print('LOADED:', heavy)\n"
        )
        assert _run_snippet(snippet) == []

    def test_auth_module_lazy_httpx_attribute(self):
        """hermes_cli.auth.httpx resolves lazily and lands in globals."""
        snippet = (
            _SNIPPET_HEADER
            + "import hermes_cli.auth as auth\n"
            + "assert 'httpx' not in sys.modules\n"
            + "_ = auth.httpx.Timeout(1)  # attribute access triggers lazy load\n"
            + "assert 'httpx' in sys.modules\n"
            + "assert auth.httpx.Timeout(1).read == 1\n"
            + "print('LOADED:', [])\n"
        )
        assert _run_snippet(snippet) == []

    def test_web_tools_httpx_patch_surface_still_works(self):
        """patch('tools.web_tools.httpx.post') keeps resolving via __getattr__."""
        snippet = (
            _SNIPPET_HEADER
            + "from unittest.mock import patch\n"
            + "import tools.web_tools as wt\n"
            + "assert 'httpx' not in sys.modules\n"
            + "with patch('tools.web_tools.httpx.post', return_value='mocked') as m:\n"
            + "    import httpx\n"
            + "    assert httpx.post is m\n"
            + "print('LOADED:', [])\n"
        )
        assert _run_snippet(snippet) == []

    def test_vision_tools_httpx_patch_surface_still_works(self):
        snippet = (
            _SNIPPET_HEADER
            + "from unittest.mock import patch\n"
            + "import tools.vision_tools as vt\n"
            + "assert 'httpx' not in sys.modules\n"
            + "with patch('tools.vision_tools.httpx.AsyncClient') as m:\n"
            + "    import httpx\n"
            + "    assert httpx.AsyncClient is m\n"
            + "print('LOADED:', [])\n"
        )
        assert _run_snippet(snippet) == []

    def test_browser_providers_import_without_requests(self):
        """Legacy re-export surface imports providers without requests."""
        snippet = (
            _SNIPPET_HEADER
            + "from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider\n"
            + "from plugins.browser.browser_use.provider import BrowserUseBrowserProvider\n"
            + "from plugins.browser.firecrawl.provider import FirecrawlBrowserProvider\n"
            + "heavy = [m for m in ('requests',) if m in sys.modules]\n"
            + "print('LOADED:', heavy)\n"
        )
        assert _run_snippet(snippet) == []
