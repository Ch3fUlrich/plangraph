"""`keeper release --epoch N` releases exactly that claim epoch and never a newer one."""
from types import SimpleNamespace

from plangraph.keeper import cli_verbs


def _release(monkeypatch, **flags):
    sent = []
    monkeypatch.setattr(cli_verbs, "write", lambda session, verb, body, renderer=None: sent.append((verb, body)) or 0)
    monkeypatch.setattr(cli_verbs, "_epoch", lambda session, key: 9)
    cli_verbs.cmd_release(SimpleNamespace(key="p:I", **flags), object())
    return sent


def test_release_with_an_epoch_sends_that_epoch(monkeypatch):
    assert _release(monkeypatch, epoch=3) == [("release", {"key": "p:I", "epoch": 3})]


def test_release_without_an_epoch_takes_the_current_one(monkeypatch):
    assert _release(monkeypatch, epoch=None) == [("release", {"key": "p:I", "epoch": 9})]
    assert _release(monkeypatch) == [("release", {"key": "p:I", "epoch": 9})]
