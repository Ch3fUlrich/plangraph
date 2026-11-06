"""Exercise the real-leg driver orchestration, not just argument preflight."""
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("answer_status", ["sent", "failed"])
def test_opencode_run_requires_question_answer_and_merge(tmp_path, monkeypatch, answer_status):
    script = Path(__file__).resolve().parents[1] / "tools/e2e_devstack.py"
    spec = importlib.util.spec_from_file_location("e2e_real_run", script)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    (tmp_path / "keeper.json").write_text("{}")
    args = driver.parse_args([
        "--state", str(tmp_path), "--temporal", "127.0.0.1:17252",
        "--keeper", "http://127.0.0.1:18787", "--temporal-http",
        "http://127.0.0.1:18233", "--worker-bin", str(script),
        "--temporal-cli", str(script), "--leg", "opencode", "--model", "some/real-model"])
    events = []
    for name in ("check_keeper_config", "check_devstack", "_run", "define_item",
                 "release_claim", "run_projector"):
        monkeypatch.setattr(driver, name, lambda *a, **kw: None)
    monkeypatch.setattr(driver, "add_principal", lambda *a: "test-token")
    monkeypatch.setattr(driver, "claim_item", lambda *a: 1)
    monkeypatch.setattr(driver.u, "init_repo", lambda *a: "base")
    monkeypatch.setattr(driver.u, "git", lambda *a: "merged")
    monkeypatch.setattr(driver.u, "set_current_version", lambda *a: None)
    def worker(binary, env, output):
        assert "stub-bin" not in env["PATH"]
        assert env.get("HOME") == os.environ.get("HOME")
        events.append("worker")
        return SimpleNamespace(proc=SimpleNamespace(pid=123))
    monkeypatch.setattr(driver.u, "WorkerProc", worker)
    monkeypatch.setattr(driver, "stop_worker", lambda *a: events.append("stopped"))
    def call(keeper, method, path, **kw):
        if path == "/v1/ready": return 200, {}
        if path.startswith("/v1/questions?"):
            events.append("question")
            return 200, {"questions": [{"id": 7, "state": "open"}]}
        assert path == "/v1/answer_question"
        assert kw["body"] == {"question_id": 7, "text": f"use {driver.u.ANSWER_TOKEN}"}
        events.append("answer")
        return 200, {"signal": {"status": answer_status}}
    monkeypatch.setattr(driver.u, "call", call)
    monkeypatch.setattr(driver, "read_attempts", lambda *a: [
        {"n": 1, "exit_class": "needs_input"}, {"n": 2, "exit_class": "ok"}])
    monkeypatch.setattr(driver, "read_audit", lambda *a: [
        {"action": verb, "ok": True, "key": "test", "code": "ok"}
        for verb in ("claim", "worker_attempt", "worker_complete", "lock_acquire", "lock_release")])
    class Handle:
        async def result(self):
            assert events[-1] == "answer" and answer_status == "sent"
            events.append("merged")
            return {"outcome": "done", "detail": "merged", "merged_sha": "merged"}
    class Client:
        @staticmethod
        async def connect(address): return Client()
        async def start_workflow(self, name, card, **kw):
            assert card["legs"][0]["model"] == "some/real-model"
            assert "PG_QUESTION:" in card["claim"]["prompt"]
            assert driver.u.ANSWER_TOKEN not in card["claim"]["prompt"]
            return Handle()
    monkeypatch.setitem(sys.modules, "temporalio.client", SimpleNamespace(Client=Client))
    assert driver.run(args) == (0 if answer_status == "sent" else 1)
    assert events == (["worker", "question", "answer", "merged", "stopped"]
                      if answer_status == "sent" else ["worker", "question", "answer", "stopped"])
