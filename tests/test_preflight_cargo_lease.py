"""The shared Cargo lease: holders named in refusals, symlinked targets, one lock per target."""
import os
import subprocess
import sys

import pytest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "initial"], check=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/locks")
def test_cargo_lease_holders_name_live_pids_and_notes_the_refusal(tmp_path, monkeypatch):
    from plangraph.spawner import host_ready as h
    target = tmp_path / "shared"
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    assert h.cargo_lease_holders(target) == [] and h.lease_holder_note(target) == ""
    lease = h.cargo_lease()
    try:
        assert h.cargo_lease_holders(target) == [os.getpid()]
        assert h.lease_holder_note(target) == f" (held by pid {os.getpid()})"
    finally:
        lease.close()
    assert h.cargo_lease_holders(target) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/locks")
def test_cap_refusal_names_the_holder_of_a_symlinked_shared_target(repo, monkeypatch):
    """The native shim leases the resolved target, so the holder is found through the symlink."""
    from plangraph.spawner import host_ready as h
    real = repo / "real-target"
    (real / "debug").mkdir(parents=True)
    (real / "debug" / "blob").write_bytes(b"x" * 65536)
    link = repo / "link-target"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(link))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "0.000001")
    monkeypatch.setattr(h, "disk_free_gb", lambda path: 100.0)
    lease = h._cargo_lease_for(real)
    try:
        assert h.cargo_lease_holders(link) == [os.getpid()]
        refusal = h.running_disk_refusal(repo)
    finally:
        lease.close()
    assert refusal is not None and refusal.startswith("cargo_target_cap:"), refusal
    assert f"(held by pid {os.getpid()})" in refusal, refusal


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/locks")
def test_leases_through_a_symlink_and_its_real_path_contend(tmp_path, monkeypatch):
    """Preflight leases the configured symlink, the shim and host reclaim the real path: one lock."""
    from plangraph.spawner import host_ready as h
    real = tmp_path / "real-target"
    real.mkdir()
    link = tmp_path / "link-target"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(link))
    shared = h.cargo_lease()
    try:
        with pytest.raises(OSError):
            h._cargo_lease_for(real, exclusive=True)
        with pytest.raises(OSError):
            h._cargo_lease_for(link, exclusive=True)
        assert h.cargo_lease_holders(real) == [os.getpid()]
        assert h.cargo_lease_holders(link) == [os.getpid()]
    finally:
        shared.close()
    h._cargo_lease_for(real, exclusive=True).close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/locks")
def test_running_disk_refusal_cap_message_names_holding_pid(repo, monkeypatch):
    """A cap refusal names the pid holding the shared cargo lease (LE-RUN-SUPERVISOR-HOLDS-CARGO-LEASE-BLOCKS-TRIM)."""
    from plangraph.spawner import host_ready as h
    target = repo / "shared-cap"
    (target / "debug").mkdir(parents=True)
    (target / "debug" / "blob").write_bytes(b"x" * 65536)
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(target))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "0.000001")
    monkeypatch.setattr(h, "disk_free_gb", lambda path: 100.0)
    lease = h.cargo_lease()
    try:
        refusal = h.running_disk_refusal(repo)
    finally:
        lease.close()
    assert refusal is not None and refusal.startswith("cargo_target_cap:"), refusal
    assert f"(held by pid {os.getpid()})" in refusal, refusal
    after = h.running_disk_refusal(repo)
    assert after is not None and "held by pid" not in after, after
