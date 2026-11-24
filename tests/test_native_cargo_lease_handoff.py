"""The native launch caller holds no shared Cargo lease across the supervisor (LE-NATIVE-PUSH-CARGO-TRIM-HANDOFF)."""
import ast
import asyncio
import inspect
import os
import sys
import textwrap

import pytest

from plangraph.spawner import overflow


def test_native_spawn_takes_no_cargo_lease_so_the_supervisor_can_reclaim_before_its_push():
    tree = ast.parse(textwrap.dedent(inspect.getsource(overflow.native_spawn)))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "cargo_lease" not in names
    assert "cargo_child_env" in names


def _fake_supervisor(tmp_path):
    """Stands in for `pg-spawnd`: answers the tool probe, then probes the lease as the supervisor would."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import fcntl, os, sys\n"
        "target = os.environ['CARGO_TARGET_DIR']\n"
        "lock = open(target + '.plangraph-lock', 'a+b')\n"
        "try:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    state = 'free'\n"
        "except OSError:\n"
        "    state = 'held'\n"
        "open(sys.argv[1], 'w').write(state + '\\n' + target + '\\n' + os.environ.get('SCCACHE_DIR', '') + '\\n')\n"
    )
    binary = tmp_path / "pg-spawnd"
    binary.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  native-tools) echo '{\"sandbox_ok\": true}';;\n"
        f"  native-writer) {sys.executable} {probe} {tmp_path}/seen;;\n"
        "esac\n"
    )
    binary.chmod(0o755)
    return binary


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/locks")
def test_a_launched_native_run_finds_the_shared_cargo_lease_free(tmp_path, monkeypatch):
    """The launch path leaves the lease to the supervisor's own Cargo commands, so a trim can take it."""
    from plangraph import fleet_snapshot
    from plangraph.spawner import cli, host_ready

    target = tmp_path / "shared-target"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    monkeypatch.setattr(fleet_snapshot, "_projection_binary", lambda: str(_fake_supervisor(tmp_path)))
    service = cli.SpawnService(state=tmp_path / "state")
    card = {"repo": str(repo), "run_kind": "native-codex"}

    asyncio.run(overflow.native_spawn(service, card, {"role": "implement", "prompt": "unused"}))

    state, seen_target, _ = (tmp_path / "seen").read_text().splitlines()
    assert state == "free", "the launching process held the shared lease while the supervisor ran"
    assert seen_target == str(target)
    # The same probe sees a holder when a Cargo command (a shared lease) is running.
    with host_ready.cargo_lease():
        assert host_ready.cargo_lease_holders(target) == [os.getpid()]
