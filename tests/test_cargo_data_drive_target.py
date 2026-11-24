"""A host with a data drive points every launcher at it by one setting (LE-DELL-DISK-EMERGENCY-SIXTH-TARGET-ON-ROOT)."""
import sys

import pytest

from plangraph.spawner import host_ready as h


def test_spawner_resolves_the_configured_target_and_never_creates_the_home_cache(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data" / "cargo-target"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(data))
    assert h.shared_cargo_target() == data
    env = h.cargo_child_env({"PATH": "/usr/bin"})
    assert env["CARGO_TARGET_DIR"] == str(data)
    if sys.platform.startswith("linux"):
        lease = h.cargo_lease()
        lease.close()
        assert (tmp_path / "data" / "cargo-target.plangraph-lock").exists()
    assert not (home / ".cache").exists()


def test_unset_setting_keeps_the_default_under_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLANGRAPH_CARGO_TARGET_DIR", raising=False)
    assert h.shared_cargo_target() == tmp_path / ".cache/plangraph/cargo-target"


@pytest.mark.parametrize("value", ["~/data/target", "/mnt/data/cargo"])
def test_home_expansion_and_absolute_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", value)
    expected = tmp_path / "data/target" if value.startswith("~") else __import__("pathlib").Path(value)
    assert h.shared_cargo_target() == expected
