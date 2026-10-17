"""Browser-test infrastructure (UI-BROWSER-TESTS W1): headless Chromium only.

Serves the page rendered from ``tests/fixtures/ui_snapshot.json`` over
``http://127.0.0.1`` and fulfils the two CDN scripts (cytoscape 3.34.0,
elkjs 0.9.3 — see ``plangraph.render`` ``cdn_script_tags``/``CDN_LIBS``/
``CDN_ELK_URL``) from local files, so a test can never reach the network.
Console discipline: any ``error`` message or pageerror fails the test;
``warning`` messages must match ``allow_warnings.txt``.
"""
from __future__ import annotations

import functools
import json
import re
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from plangraph.render import CDN_BASE, CDN_ELK_URL, CDN_LIBS, render  # noqa: E402

SNAPSHOT_PATH = ROOT / "tests" / "fixtures" / "ui_snapshot.json"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ALLOW_WARNINGS_PATH = Path(__file__).resolve().parent / "allow_warnings.txt"

SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "phone": (390, 844)}

#: Mark a test with every viewport size (indirectly parametrizes the ``size``
#: fixture, so the ``page`` fixture picks it up).
for_sizes = pytest.mark.parametrize(
    "size", list(SIZES.values()), ids=list(SIZES.keys()), indirect=True
)


def _cdn_url_to_local() -> dict[str, Path]:
    mapping = {
        f"{CDN_BASE}/{library}/{version}/{filename}": ROOT / "vendor" / "cytoscape.min.js"
        for library, version, filename in CDN_LIBS
    }
    mapping[CDN_ELK_URL] = ASSETS_DIR / "elk.bundled.js"
    return mapping


@pytest.fixture(scope="session")
def vendor_assets():
    """CDN URL -> local file that must serve it. Session-wide (files are static)."""
    mapping = _cdn_url_to_local()
    for url, path in mapping.items():
        assert path.is_file(), f"local vendor asset missing for {url}: {path}"
    return mapping


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    """Render the fixture snapshot (cdn=True) and serve it from 127.0.0.1."""
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    document = render(snapshot, ROOT / "vendor", cdn=True)
    web_root = tmp_path_factory.mktemp("pg-site")
    (web_root / "index.html").write_text(document, encoding="utf-8")
    # page_kg.js fetches this sibling on boot; a disabled export is the designed
    # "nothing to show" state (a 404 would be a console *error*).
    (web_root / "kg-export.json").write_text('{"enabled": false}', encoding="utf-8")

    class _Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_Quiet, directory=str(web_root))
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def size(request):
    """Viewport (width, height); override per test via ``for_sizes``/parametrize."""
    return getattr(request, "param", None) or SIZES["desktop"]


def _allowed_warning_res():
    out = []
    if ALLOW_WARNINGS_PATH.is_file():
        for line in ALLOW_WARNINGS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            pattern = line.split("# owner:", 1)[0].strip()
            if pattern:
                out.append(re.compile(pattern))
    return out


@pytest.fixture
def page(browser, site, vendor_assets, size):
    """Playwright page on the local site: no request may leave 127.0.0.1, and
    console/pageerror violations fail the test at teardown."""
    width, height = size
    context = browser.new_context(viewport={"width": width, "height": height})
    pg = context.new_page()
    console_messages: list[tuple[str, str]] = []
    page_errors: list[str] = []
    stray_requests: list[str] = []
    allowed: list[tuple[str, re.Pattern]] = []

    def pg_allow_console(kind: str, pattern: str) -> None:
        """Declare a console message THIS test deliberately provokes (e.g. an
        induced library failure, or the warning the test itself asserts on) so
        the teardown discipline below does not double-count it. Narrow pattern,
        owner in the caller's comment."""
        allowed.append((kind, re.compile(pattern)))

    pg.pg_allow_console = pg_allow_console
    pg.on("console", lambda msg: console_messages.append((msg.type, msg.text)))
    pg.on("pageerror", lambda err: page_errors.append(str(err)))

    def _handle(route, request):
        url = request.url
        host = urlparse(url).hostname
        if host == "127.0.0.1":
            if url.endswith("/favicon.ico"):  # avoid a 404 console error
                route.fulfill(status=204, content_type="image/x-icon", body=b"")
            else:
                route.continue_()
            return
        local = vendor_assets.get(url)
        if local is not None:
            route.fulfill(status=200, content_type="application/javascript",
                          body=local.read_bytes())
            return
        stray_requests.append(url)
        route.abort()

    pg.route("**/*", lambda route: _handle(route, route.request))
    try:
        yield pg
    finally:
        context.close()
        failures = []

        def _expected(kind: str, text: str) -> bool:
            return any(k == kind and rx.search(text) for k, rx in allowed)

        for kind, text in console_messages:
            if _expected(kind, text):
                continue
            if kind == "error":
                failures.append(f"console error: {text}")
            elif kind in ("warning", "warn") and not any(
                r.search(text) for r in _allowed_warning_res()
            ):
                failures.append(f"console warning (not in allow_warnings.txt): {text}")
        failures += [f"pageerror: {e}" for e in page_errors
                     if not _expected("pageerror", e)]
        failures += [f"external request aborted (tests must not hit a CDN): {u}"
                     for u in stray_requests]
        if failures:
            pytest.fail("\n".join(failures))
