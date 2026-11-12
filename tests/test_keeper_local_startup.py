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
        assert other.get("/v1/health").status == 200
        own = tmp_path / "own"
        own.mkdir()
        ready = own / "ready.json"
        # A stale ready file points at a live keeper whose health would pass.
        ready.write_bytes((tmp_path / "other" / "ready.json").read_bytes())
        real_popen = harness.subprocess.Popen
        launches = []

        def popen(args, **kwargs):
            if args[1:3] == ["-m", "plangraph.keeper.daemon"]:
                assert args[args.index("--port") + 1] == "0"
                assert Path(args[args.index("--ready-file") + 1]) == ready
                # Check before the child can overwrite the stale endpoint.
                assert not ready.exists()
                launches.append(args)
            return real_popen(args, **kwargs)

        real_get = harness.Keeper.get
        health_urls = []

        def get(self, path, *args, **kwargs):
            if self.dir == own and path == "/v1/health":
                assert self.url != other.url
                endpoint = json.loads(ready.read_text(encoding="utf-8"))
                assert self.url == f"http://{endpoint['host']}:{endpoint['port']}"
                health_urls.append(self.url)
            return real_get(self, path, *args, **kwargs)

        monkeypatch.setattr(harness.subprocess, "Popen", popen)
        monkeypatch.setattr(harness.Keeper, "get", get)
        keeper = harness.start_local(own)
        assert len(launches) == 1
        assert health_urls == [keeper.url]
        check_chain(keeper)
        assert keeper.url != other.url
        endpoint = json.loads(ready.read_text(encoding="utf-8"))
        assert keeper.url == f"http://{endpoint['host']}:{endpoint['port']}"
        assert stat.S_IMODE(ready.stat().st_mode) == 0o600
    finally:
        if keeper is not None:
            keeper.stop()
        other.stop()
