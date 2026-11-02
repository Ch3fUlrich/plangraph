"""F9 [MED] coverage (D-3564): seat_verdict send/IO paths and the record
--head mismatch refusal over the real-artifact fixture run dirs.

Covers the previously untested seat_verdict.py lines: send() HTTPError path
(the 500 body is returned, main exits EXIT_IO), the --head mismatch refusal
over a record seat dir, the non-dry-run IO paths (no keeper URL, missing
reviewer token, real send success/failure) and an end-to-end dry run from the
passing free-leg fixture whose reason carries the run id."""
import io
import json
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seat_verdict as sv  # noqa: E402
from test_seat_record_fixes import (  # noqa: E402
    FIXTURES, PASS_RUN, HEAD40, argv_for)

# the passing free-leg fixture's witness model is opencode/cx-gpt-6-luna-free
FAMILY = "openai"


def fixture_seat(tmp_path, name=PASS_RUN):
    """Copy a real-artifact fixture run dir like the spawner wrote it:
    run dir 0700, record.json and live.log 0600 (umask 002 would make the
    copies group-writable and record.json would be refused)."""
    seat = tmp_path / name
    shutil.copytree(FIXTURES / name, seat)
    seat.chmod(0o700)
    for fname in ("record.json", "live.log"):
        path = seat / fname
        if path.exists():
            path.chmod(0o600)
    return seat


def token_dir(tmp_path, name="toks", token=True):
    tokens = tmp_path / name
    tokens.mkdir()
    if token:
        (tokens / f"reviewer-{FAMILY}.token").write_text("dummy\n")
    return tokens


def live_argv(seat, tokens, *, url="http://keeper.test", head=HEAD40):
    argv = ["--key", "demo:A", "--seat-dir", str(seat),
            "--writer-family", "deepseek", "--token-dir", str(tokens)]
    if url is not None:
        argv += ["--url", url]
    if head is not None:
        argv += ["--head", head]
    return argv


class FakeResponse:
    """Stand-in for the urlopen response context manager."""

    def __init__(self, status, body=b'{"ok":true}'):
        self.status = status
        self.body = body

    def read(self, size=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# 1. send() HTTPError path: the error body is returned; main exits EXIT_IO.
def test_send_http_error_body_returned_and_main_exits_io(monkeypatch, tmp_path,
                                                        capsys):
    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error",
                                     {}, io.BytesIO(b"boom"))

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    status, text = sv.send("http://keeper.test", "tok",
                           {"key": "demo:A", "ok": True, "reason": "x"})
    assert (status, text) == (500, "boom")
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path))) == sv.EXIT_IO
    assert "http=500" in capsys.readouterr().out


# 2. --head different from the record's completion.head_sha: refused, and
# stderr names both shas.
def test_head_mismatch_refusal_names_both_shas(tmp_path, capsys):
    seat = fixture_seat(tmp_path)
    tokens = token_dir(tmp_path, token=False)
    assert sv.main(live_argv(seat, tokens, url=None,
                             head="f" * 40)) == sv.EXIT_REFUSED
    err = capsys.readouterr().err
    assert HEAD40 in err and "f" * 40 in err


# 3. non-dry-run without --url and PG_URL unset.
def test_no_url_no_pg_url_exits_io(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("PG_URL", raising=False)
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path),
                             url=None)) == sv.EXIT_IO
    assert "no keeper URL" in capsys.readouterr().err


# 4. token dir lacking reviewer-<family>.token.
def test_missing_reviewer_token_exits_io(monkeypatch, tmp_path, capsys):
    def must_not_call(request, timeout=None):
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(urllib.request, "urlopen", must_not_call)
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path,
                                             token=False))) == sv.EXIT_IO
    assert "send failed" in capsys.readouterr().err


# 5. real send success: fake 200 response, token read at send time.
def test_send_success_prints_key_family_ok_http(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(200, b'{"ok":true}'))
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "key=demo:A family=openai ok=" in out
    assert "http=200" in out
    assert "dummy" not in out  # the token is never printed


# 6. urlopen raising URLError.
def test_send_url_error_exits_io(monkeypatch, tmp_path, capsys):
    def raiser(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", raiser)
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path))) == sv.EXIT_IO
    assert "send failed" in capsys.readouterr().err


# 7. fake response with status 500.
def test_send_http_500_reply_exits_io(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(500, b'{"err":"boom"}'))
    seat = fixture_seat(tmp_path)
    assert sv.main(live_argv(seat, token_dir(tmp_path))) == sv.EXIT_IO
    assert "http=500" in capsys.readouterr().out


# 8. end-to-end dry run from the passing free-leg fixture: the JSON body's
# reason names the run id.
def test_dry_run_fixture_reason_contains_run_id(tmp_path, capsys):
    seat = fixture_seat(tmp_path)
    assert sv.main(argv_for(seat)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert PASS_RUN in payload["body"]["reason"]
