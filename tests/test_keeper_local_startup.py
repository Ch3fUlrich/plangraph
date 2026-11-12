"""Local keepers must never mistake another listener's health for their own."""
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "contract"))
import contract_harness as harness
from test_keeper_http_events import (
    test_each_write_extends_the_chain_by_the_documented_rule as check_chain,
)


def test_local_startup_cannot_connect_to_another_keepers_port(tmp_path, monkeypatch):
    other = harness.start_local(tmp_path / "other")
    keeper = None
    try:
        # Deterministically occupy the port between selection and the child's bind.
        monkeypatch.setattr(harness, "_free_port", lambda: int(other.url.rsplit(":", 1)[1]),
                            raising=False)
        keeper = harness.start_local(tmp_path / "own")
        check_chain(keeper)
        assert keeper.url != other.url
        ready = tmp_path / "own" / "ready.json"
        endpoint = json.loads(ready.read_text(encoding="utf-8"))
        assert keeper.url == f"http://{endpoint['host']}:{endpoint['port']}"
        assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    finally:
        if keeper is not None:
            keeper.stop()
        other.stop()
