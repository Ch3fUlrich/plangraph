import json
import os
import time

import pytest

from plangraph import kg_export as kg


@pytest.mark.parametrize("kind", ["exception", "graph", "missing", "empty", "unset"])
def test_failure_status_and_secret_safety(tmp_path, capsys, monkeypatch, kind):
    secret = "-".join(["synthetic", "private", "credential", "fixture"])
    token = tmp_path / "credential"
    if kind != "missing":
        token.write_text("" if kind == "empty" else secret)
    env = {"PG_KG_URL": "https://omnigraph.example.invalid", "PG_KG_GRAPHS": "g"}
    if kind != "unset":
        env["PG_KG_TOKEN_FILE"] = str(token)

    def fail(*args):
        raise (kg.KgError(secret) if kind == "graph" else RuntimeError(secret))

    assert kg.run([], tmp_path, env=env, transport=fail) == 0
    status = json.loads((tmp_path / kg.STATUS_NAME).read_text())
    assert status["ok"] is False
    assert status["error_count"] == 1
    assert status["error_class"] == ("KgError" if kind == "graph" else "RuntimeError" if kind == "exception" else "DisabledMisconfig")
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AUTOOS_ALERT_INBOX", str(inbox))
    assert kg.main(["--check", str(tmp_path)]) == 1
    diagnostics = (tmp_path / kg.STATUS_NAME).read_text() + capsys.readouterr().err + inbox.read_text()
    assert secret not in diagnostics
    assert "Authorization" not in diagnostics
    if kind in {"missing", "empty", "unset"}:
        assert inbox.read_text() == "KG-EXPORT-DISABLED-MISCONFIG\n"


@pytest.mark.parametrize("state,expected", [("fresh", None), ("stale", "KG-EXPORT-STALE"),
                                              ("failed", "KG-EXPORT-FAILED KgError"),
                                              ("missing", "KG-EXPORT-STALE missing")])
def test_check_health(tmp_path, monkeypatch, capsys, state, expected):
    export = tmp_path / kg.EXPORT_NAME
    if state != "missing":
        export.write_text('{"enabled":true}')
    (tmp_path / kg.STATUS_NAME).write_text(json.dumps({"enabled": True, "ok": state != "failed",
                                                    "error_class": "KgError"}))
    if state == "stale":
        os.utime(export, (time.time() - 7201,) * 2)
    inbox = tmp_path / "inbox"
    inbox.write_text("existing\n")
    monkeypatch.setenv("AUTOOS_ALERT_INBOX", str(inbox))
    assert kg.main(["--check", str(tmp_path), "--max-age", "2h"]) == (1 if expected else 0)
    output = capsys.readouterr().out
    if expected:
        assert output.startswith(expected)
        assert len(output.splitlines()) == 1
        assert inbox.read_text() == "existing\n" + output
    else:
        assert output == ""
        assert inbox.read_text() == "existing\n"


def test_unreachable_run_overrides_fresh_export(tmp_path, monkeypatch, capsys):
    import urllib.error

    token = tmp_path / "credential"
    token.write_text("-".join(["synthetic", "private", "fixture"]))
    (tmp_path / kg.EXPORT_NAME).write_text('{"enabled":true,"nodes":{}}')

    def unreachable(*args, **kwargs):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(kg._OPENER, "open", unreachable)
    assert kg.run([], tmp_path, env={"PG_KG_URL": "https://omnigraph.example.invalid",
                  "PG_KG_TOKEN_FILE": str(token), "PG_KG_GRAPHS": "g"}) == 0
    assert json.loads((tmp_path / kg.STATUS_NAME).read_text())["kept_previous"] is True
    assert kg.main(["--check", str(tmp_path)]) == 1
    assert capsys.readouterr().out == "KG-EXPORT-FAILED KgError\n"


def test_intentionally_disabled_is_healthy(tmp_path):
    assert kg.run([], tmp_path, env={}) == 0
    assert kg.main(["--check", str(tmp_path)]) == 0


def test_write_failure_records_status_when_possible(tmp_path, monkeypatch):
    original = kg._atomic_write

    def fail_export(path, text):
        if path.name == kg.EXPORT_NAME:
            raise OSError("cannot write")
        original(path, text)

    monkeypatch.setattr(kg, "_atomic_write", fail_export)
    assert kg.run([], tmp_path, env={}) == 0
    status = json.loads((tmp_path / kg.STATUS_NAME).read_text())
    assert status["ok"] is False
    assert status["error_class"] == "OSError"
    assert status["error_count"] == 1
