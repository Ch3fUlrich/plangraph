"""Hermetic tests for the Forgejo commit-status adapter.

A ``http.server`` on ``127.0.0.1`` (ephemeral port, thread) speaks the same
observable contract as the swagger excerpt shipped with the writer brief
(``forgejo-statuses-api.json``, every claim below cites its line numbers):
path ``/api/v1/repos/{owner}/{repo}/commits/{sha}/statuses``, ``page``/
``limit`` query paging without a ``Link`` header, ``Authorization: token``,
a 200 JSON list of ``CommitStatus`` with the documented field names, and a
404 JSON error object. No test leaves the loopback interface.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from evidence_fakes import ITEM, make_ctx
from plangraph.keeper import config as keeper_config
from plangraph.keeper import evidence as ev
from plangraph.keeper import evidence_ci
from plangraph.keeper.evidence_ci import CIAdapter
from plangraph.keeper.evidence_forgejo import ForgejoStatusAdapter, normalise_statuses

SHA = "c" * 40
OWNER = "acme"
REPO = "widgets"
SLUG = f"{OWNER}/{REPO}"
AUTH_TOKEN = "fgj-fake-value-0123456789"
# The canonical request path the fake serves (owner/repo/sha fixed per test).
_EXPECT = f"/api/v1/repos/{OWNER}/{REPO}/commits/{SHA}/statuses"


# --------------------------------------------------------------------------- #
# fake Forgejo statuses endpoint
# --------------------------------------------------------------------------- #
class _Server(ThreadingHTTPServer):
    daemon_threads = True  # a client that timed out must not block teardown

    def handle_error(self, request, client_address):
        return  # closed sockets after a client-side timeout are expected


def _handler(state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return  # keep pytest output clean

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["requests"].append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                }
            )
            if state["sleep"]:
                time.sleep(state["sleep"])
            if self.headers.get("Authorization") != f"token {state['token']}":
                self._send(401, {"message": "unauthorized"})
                return
            parts = urlsplit(self.path)
            if parts.path != state.get("expect", _EXPECT):
                self._send(404, {"errors": None, "message": "status not found"})
                return
            if state["code"]:
                self._send(state["code"], {"message": "boom"})
                return
            if state["raw"] is not None:
                body = state["raw"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            query = parse_qs(parts.query)
            page = int(query.get("page", ["1"])[0])
            limit = int(query.get("limit", ["50"])[0])
            start = (page - 1) * limit
            self._send(200, state["statuses"][start:start + limit])

    return Handler


@pytest.fixture
def forgejo_server():
    """``start(...) -> state``: a fresh 127.0.0.1 server per call, all stopped."""
    servers: list[_Server] = []

    def start(statuses=(), *, token=AUTH_TOKEN, sleep=0.0, raw=None, code=0, expect=None):
        state = {
            "statuses": list(statuses),
            "token": token,
            "sleep": sleep,
            "raw": raw,
            "code": code,
            "requests": [],
        }
        if expect is not None:
            state["expect"] = expect
        httpd = _Server(("127.0.0.1", 0), _handler(state))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        state["base_url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
        return state

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "forgejo-ci-read.token"
    path.write_text(AUTH_TOKEN + "\n", encoding="utf-8")
    return path


def _status(id_, context, status, *, created="2026-10-16T00:00:00Z", updated=None):
    """One ``CommitStatus`` with exactly the api json field names."""
    return {
        "id": id_,
        "status": status,
        "context": context,
        "created_at": created,
        "updated_at": updated or created,
        "target_url": "https://example.invalid/build",
        "description": "",
    }


def _adapter(base_url, token_file, **kw) -> ForgejoStatusAdapter:
    return ForgejoStatusAdapter(base_url, str(token_file), **kw)


# --------------------------------------------------------------------------- #
# green / red through CIAdapter + _ci_conclusion
# --------------------------------------------------------------------------- #
def test_all_success_is_green_through_ci_adapter(forgejo_server, token_file):
    state = forgejo_server(
        [_status(1, "build", "success"), _status(2, "test", "success")]
    )
    ci = CIAdapter({"routing": f"forgejo:{SLUG}"}, forgejo=_adapter(state["base_url"], token_file))
    # mode() hands the stored value back untouched; no parsing, no rewriting.
    assert ci.mode("routing") == f"forgejo:{SLUG}"
    runs = ci.check_runs(f"forgejo:{SLUG}", SHA)
    assert ev._ci_conclusion(runs, SHA) is None
    assert len(runs) == 2
    run = runs[0]
    assert run["head_sha"] == SHA
    assert run["app"] == {"slug": "forgejo-actions"}
    assert run["status"] == "completed" and run["conclusion"] == "success"
    # The real endpoint contract: documented path, 1-based page + limit query,
    # and the token only in the Authorization header.
    request = state["requests"][0]
    assert request["path"] == f"{_EXPECT}?page=1&limit=50"
    assert request["auth"] == f"token {AUTH_TOKEN}"


def test_commit_ci_verifier_passes_end_to_end(forgejo_server, token_file):
    state = forgejo_server([_status(1, "build", "success")])
    ci = CIAdapter({"routing": f"forgejo:{SLUG}"},
                   forgejo=_adapter(state["base_url"], token_file))
    res = ev.VERIFIERS["commit+ci"].verify(ITEM, {"commit": SHA}, make_ctx(ci=ci))
    assert res.ok is True, res.detail


def test_one_failure_is_not_green(forgejo_server, token_file):
    state = forgejo_server(
        [_status(1, "build", "success"), _status(2, "test", "failure")]
    )
    runs = _adapter(state["base_url"], token_file).check_runs(SLUG, SHA)
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None and "not green" in detail


def test_pending_status_reports_pending(forgejo_server, token_file):
    state = forgejo_server([_status(1, "build", "pending")])
    runs = _adapter(state["base_url"], token_file).check_runs(SLUG, SHA)
    assert runs[0]["status"] == "in_progress"
    assert runs[0]["conclusion"] is None
    detail = ev._ci_conclusion(runs, SHA)
    assert detail is not None and "pending" in detail


def test_newest_status_per_context_wins(forgejo_server, token_file):
    # The API lists every status ever posted: an old failure of a rerun must
    # not block a context whose newest status is a success.
    state = forgejo_server(
        [
            _status(5, "build", "failure",
                    created="2026-10-16T00:00:00Z", updated="2026-10-16T00:00:00Z"),
            _status(9, "build", "success",
                    created="2026-10-16T01:00:00Z", updated="2026-10-16T01:00:00Z"),
            _status(6, "test", "success", updated="2026-10-16T00:30:00Z"),
        ]
    )
    runs = _adapter(state["base_url"], token_file).check_runs(SLUG, SHA)
    assert len(runs) == 2  # one per context, the stale failure dropped
    assert ev._ci_conclusion(runs, SHA) is None


def test_no_statuses_returns_empty_and_fails(forgejo_server, token_file):
    state = forgejo_server([])
    runs = _adapter(state["base_url"], token_file).check_runs(SLUG, SHA)
    assert runs == []
    assert ev._ci_conclusion(runs, SHA) is not None  # verifier fails on empty


# --------------------------------------------------------------------------- #
# normalise_statuses (pure)
# --------------------------------------------------------------------------- #
def test_normalise_unknown_status_fails_closed():
    runs = normalise_statuses(SHA, [_status(1, "build", "weird-value")])
    assert runs[0]["conclusion"] == "failure"
    assert runs[0]["head_sha"] == SHA


def test_normalise_status_conclusions():
    runs = normalise_statuses(
        SHA,
        [
            _status(1, "a", "success"),
            _status(2, "b", "error"),
            _status(3, "c", "warning"),
            _status(4, "d", "skipped"),
            _status(5, "e", "pending"),
        ],
    )
    by_name = {run["name"]: run for run in runs}
    assert by_name["a"]["conclusion"] == "success"
    assert by_name["b"]["conclusion"] == "failure"
    assert by_name["c"]["conclusion"] == "neutral"
    assert by_name["d"]["conclusion"] == "skipped"
    assert by_name["e"]["conclusion"] is None
    assert by_name["e"]["status"] == "in_progress"
    assert all(by_name[n]["status"] == "completed" for n in "abcd")


def test_normalise_tie_on_updated_at_breaks_by_id():
    # Same context, same updated_at: the higher id is the newer write, so an
    # id-rank tie-break must not let the older failure win.
    runs = normalise_statuses(
        SHA,
        [
            _status(7, "build", "failure", updated="2026-10-16T02:00:00Z"),
            _status(3, "build", "success", updated="2026-10-16T02:00:00Z"),
        ],
    )
    assert len(runs) == 1
    assert runs[0]["conclusion"] == "failure"
    assert runs[0]["id"] == 7


def test_normalise_empty_and_non_list():
    assert normalise_statuses(SHA, []) == []
    assert normalise_statuses(SHA, {"total": 0}) == []


# --------------------------------------------------------------------------- #
# paging, limits, transport failures
# --------------------------------------------------------------------------- #
def test_short_page_ends_paging(forgejo_server, token_file):
    state = forgejo_server(
        [_status(1, "a", "success"), _status(2, "b", "success"),
         _status(3, "c", "success")]
    )
    adapter = _adapter(state["base_url"], token_file, page_size=2)
    assert len(adapter.check_runs(SLUG, SHA)) == 3
    assert len(state["requests"]) == 2  # second page was short -> stop
    assert "page=2" in state["requests"][1]["path"]


def test_full_pages_at_max_pages_raise_truncation(forgejo_server, token_file):
    # No Link header and no total: a full page at the cap may hide more
    # statuses, so the adapter refuses instead of returning a partial list.
    state = forgejo_server(
        [_status(i, f"ctx{i}", "success") for i in range(1, 5)]
    )
    adapter = _adapter(state["base_url"], token_file, page_size=2, max_pages=2)
    with pytest.raises(RuntimeError, match="truncated"):
        adapter.check_runs(SLUG, SHA)
    assert len(state["requests"]) == 2  # it never asked for page 3


def test_oversize_body_is_refused(forgejo_server, token_file):
    state = forgejo_server(raw=b"[" + b"0," * 40 + b"0]")
    adapter = _adapter(state["base_url"], token_file, max_bytes=64)
    with pytest.raises(RuntimeError, match="too large"):
        adapter.check_runs(SLUG, SHA)


def test_timeout_is_refused(forgejo_server, token_file):
    state = forgejo_server([_status(1, "build", "success")], sleep=2.0)
    adapter = _adapter(state["base_url"], token_file, timeout=0.25)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.check_runs(SLUG, SHA)
    assert AUTH_TOKEN not in str(excinfo.value)


def test_http_error_carries_code_and_slug_only(forgejo_server, token_file):
    state = forgejo_server([_status(1, "build", "success")], code=500)
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.check_runs(SLUG, SHA)
    text = str(excinfo.value)
    assert "HTTP 500" in text and OWNER in text and REPO in text
    assert AUTH_TOKEN not in text and "boom" not in text


def test_404_json_error_is_a_sanitised_failure(forgejo_server, token_file):
    # The fake only serves the canonical owner/repo/sha path (api json line 80).
    state = forgejo_server([_status(1, "build", "success")])
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.check_runs(f"{OWNER}/elsewhere", SHA)
    assert "HTTP 404" in str(excinfo.value)
    assert AUTH_TOKEN not in str(excinfo.value)


def test_non_json_body_is_refused(forgejo_server, token_file):
    state = forgejo_server(raw=b"<html>not json</html>")
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(RuntimeError, match="not JSON"):
        adapter.check_runs(SLUG, SHA)


def test_non_list_body_is_refused(forgejo_server, token_file):
    state = forgejo_server(raw=b'{"message": "oops"}')
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(RuntimeError, match="not a list"):
        adapter.check_runs(SLUG, SHA)


def test_server_rejecting_the_token_is_a_sanitised_failure(
    forgejo_server, tmp_path
):
    # The server expects AUTH_TOKEN; the adapter carries a different one, so the
    # 401 must not drag the token (ours or theirs) back out in the message.
    state = forgejo_server([_status(1, "build", "success")])
    other = tmp_path / "other.token"
    other.write_text("not-the-server-token\n", encoding="utf-8")
    adapter = _adapter(state["base_url"], other)
    with pytest.raises(RuntimeError) as excinfo:
        adapter.check_runs(SLUG, SHA)
    text = str(excinfo.value)
    assert "HTTP 401" in text and SLUG in text
    assert "not-the-server-token" not in text and AUTH_TOKEN not in text


# --------------------------------------------------------------------------- #
# token handling
# --------------------------------------------------------------------------- #
def test_token_only_in_header_never_in_repr_logs_or_url(
    forgejo_server, token_file, caplog
):
    state = forgejo_server([_status(1, "build", "success")])
    adapter = _adapter(state["base_url"], token_file)
    with caplog.at_level(logging.DEBUG, logger="plangraph"):
        runs = adapter.check_runs(SLUG, SHA)
    assert runs and ev._ci_conclusion(runs, SHA) is None
    assert AUTH_TOKEN not in repr(adapter)
    assert AUTH_TOKEN not in caplog.text
    request = state["requests"][0]
    assert request["auth"] == f"token {AUTH_TOKEN}"  # the header carries it...
    assert AUTH_TOKEN not in request["path"]  # ...and the URL never does


def test_token_file_missing_is_refused_before_any_request(forgejo_server, tmp_path):
    state = forgejo_server([_status(1, "build", "success")])
    adapter = _adapter(state["base_url"], tmp_path / "absent.token")
    with pytest.raises(RuntimeError):
        adapter.check_runs(SLUG, SHA)
    assert state["requests"] == []


def test_token_file_empty_is_refused(forgejo_server, tmp_path):
    state = forgejo_server([_status(1, "build", "success")])
    empty = tmp_path / "empty.token"
    empty.write_text("   \n", encoding="utf-8")
    adapter = _adapter(state["base_url"], empty)
    with pytest.raises(RuntimeError, match="empty"):
        adapter.check_runs(SLUG, SHA)
    assert state["requests"] == []


def test_token_file_symlink_is_refused(forgejo_server, token_file, tmp_path):
    state = forgejo_server([_status(1, "build", "success")])
    link = tmp_path / "link.token"
    try:
        os.symlink(token_file, link)
    except (OSError, NotImplementedError):  # pragma: no cover - host dependent
        pytest.skip("symlink creation not permitted on this host")
    adapter = _adapter(state["base_url"], link)
    with pytest.raises(RuntimeError, match="symlink"):
        adapter.check_runs(SLUG, SHA)
    assert state["requests"] == []


def test_token_never_in_the_refusal_message(token_file, tmp_path):
    # A whitespace-bearing token is refused outright, and its text stays out.
    bad = tmp_path / "bad.token"
    bad.write_text("two words\n", encoding="utf-8")
    adapter = ForgejoStatusAdapter("https://forgejo.example.invalid", str(bad))
    with pytest.raises(RuntimeError) as excinfo:
        adapter.check_runs(SLUG, SHA)
    assert "two words" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# input validation before any request
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad_slug", [
    "acme",
    "acme/widgets/extra",
    "../../etc/passwd",
    "acme/widgets?state=failure",
    "acme/widgets#frag",
    "",
    None,
    7,
])
def test_bad_slug_is_refused_before_any_request(forgejo_server, token_file, bad_slug):
    state = forgejo_server([_status(1, "build", "success")])
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(ValueError):
        adapter.check_runs(bad_slug, SHA)
    assert state["requests"] == []


@pytest.mark.parametrize("bad_sha", [
    "c" * 39,        # too short: abbreviations are not commit refs
    "C" * 40,        # upper case
    "HEAD",          # a ref, not a sha
    "c" * 40 + "\n", # trailing newline must not slip through
    "",
    None,
])
def test_bad_sha_is_refused_before_any_request(forgejo_server, token_file, bad_sha):
    state = forgejo_server([_status(1, "build", "success")])
    adapter = _adapter(state["base_url"], token_file)
    with pytest.raises(ValueError):
        adapter.check_runs(SLUG, bad_sha)
    assert state["requests"] == []


def test_plain_slug_value_still_takes_the_gh_path(
    monkeypatch, forgejo_server, token_file
):
    # A configured forgejo adapter must not hijack the ordinary gh flow.
    state = forgejo_server([_status(1, "build", "success")])
    paths: list[str] = []

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["gh", "api"], cmd
        paths.append(cmd[2])
        payload = {
            "total_count": 1,
            "check_runs": [{
                "id": 1, "name": "build", "status": "completed",
                "conclusion": "success", "started_at": "2026-10-16T00:00:00Z",
                "head_sha": SHA, "app": {"slug": "circleci"},
                "check_suite": {"id": 111},
            }],
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
    ci = CIAdapter({"routing": "owner/repo"},
                   forgejo=_adapter(state["base_url"], token_file))
    runs = ci.check_runs("owner/repo", SHA)
    assert state["requests"] == []  # the Forgejo endpoint was never asked
    assert paths and paths[0].startswith(f"repos/owner/repo/commits/{SHA}/check-runs")
    assert ev._ci_conclusion(runs, SHA) is None


def test_forgejo_value_without_adapter_configured_raises():
    ci = CIAdapter({"invest": f"forgejo:{SLUG}"})
    with pytest.raises(RuntimeError, match="forgejo"):
        ci.check_runs(f"forgejo:{SLUG}", SHA)


# --------------------------------------------------------------------------- #
# base URL and config wiring
# --------------------------------------------------------------------------- #
def test_plain_http_away_from_loopback_is_refused(token_file):
    with pytest.raises(RuntimeError, match="https"):
        ForgejoStatusAdapter("http://forgejo.example.invalid", str(token_file))


def test_credentials_in_base_url_are_refused(token_file):
    with pytest.raises(RuntimeError, match="credentials"):
        ForgejoStatusAdapter("https://user:pass@forgejo.example.invalid", str(token_file))


def test_default_context_builds_the_adapter_only_with_both_keys(token_file):
    cfg = {
        "ci": {"invest": f"forgejo:{SLUG}"},
        "forgejo_url": "https://forgejo.example.invalid",
        "forgejo_token_file": str(token_file),
    }
    assert ev.default_context(cfg).ci._forgejo is not None
    # One key missing -> no adapter -> the value fails closed in check_runs.
    assert ev.default_context({"ci": cfg["ci"],
                               "forgejo_url": cfg["forgejo_url"]}).ci._forgejo is None
    assert ev.default_context({"ci": cfg["ci"],
                               "forgejo_token_file": str(token_file)}).ci._forgejo is None
    assert ev.default_context({"ci": cfg["ci"]}).ci._forgejo is None


def test_config_stores_the_keys_and_repr_never_shows_the_values(tmp_path):
    data = {
        "ci": {"invest": f"forgejo:{SLUG}"},
        "forgejo_url": "https://forgejo.example.invalid",
        "forgejo_token_file": "/path/to/forgejo-ci-read.token",
    }
    cfg = keeper_config.Config(data, tmp_path)
    assert cfg.forgejo_url == "https://forgejo.example.invalid"
    assert cfg.forgejo_token_file == "/path/to/forgejo-ci-read.token"
    text = repr(cfg)
    assert "forgejo.example.invalid" not in text
    assert "/path/to/forgejo-ci-read.token" not in text
    assert "forgejo_token_file" in text  # key names are fine, values are not
