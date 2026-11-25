"""Work-drive and build-drive admission: drive choice, mounts, and the running guard's target."""
import os

import pytest

from plangraph.spawner import host_ready as h
from plangraph.spawner import host_ready_source as source


def _free(table):
    def free(path):
        if isinstance(table[path], Exception):
            raise table[path]
        return table[path]
    return free


def test_an_unreadable_secondary_drive_does_not_stop_a_healthy_one():
    free = _free({'/bad': OSError('mount gone'), '/ok': 30.0})
    assert source.select_drive([('/bad', 5.0), ('/ok', 5.0)], lambda: None, free) == ('/ok', None)
    with pytest.raises(OSError, match='mount gone'):
        source.select_drive([('/bad', 5.0)], lambda: None, free)


def test_build_drives_set_the_cargo_target_and_enforce_their_floor(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    for name in 'ab':
        (tmp_path / name).mkdir()
    policy = {'build_drives': [{'path': str(tmp_path / 'a'), 'disk_floor_gb': 20},
                               {'path': str(tmp_path / 'b'), 'disk_floor_gb': 20}]}
    free = {str(tmp_path / 'a'): 30.0, str(tmp_path / 'b'): 90.0}
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free[path])
    assert h.shared_cargo_target(policy) == tmp_path / 'b' / 'cargo-target'
    free[str(tmp_path / 'b')] = 10.0
    assert h.shared_cargo_target(policy) == tmp_path / 'a' / 'cargo-target'
    free[str(tmp_path / 'a')] = 19.0
    with pytest.raises(OSError, match='disk_low: no build drive meets its floor'):
        h.shared_cargo_target(policy)
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', str(tmp_path / 'env'))
    assert h.shared_cargo_target(policy) == tmp_path / 'env'


def test_a_missing_build_mount_is_not_measured_through_its_parent(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 500.0)
    policy = {'build_drives': [{'path': str(tmp_path / 'absent'), 'disk_floor_gb': 20}]}
    with pytest.raises(OSError, match='absent: not mounted'):
        h.shared_cargo_target(policy)


def test_an_existing_directory_on_an_unmounted_drive_does_not_qualify(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    (tmp_path / 'g').mkdir()
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 500.0)
    entry = {'path': str(tmp_path / 'g'), 'mount': str(tmp_path / 'g'), 'disk_floor_gb': 20}
    with pytest.raises(OSError, match='not mounted'):
        h.shared_cargo_target({'build_drives': [entry]})
    monkeypatch.setattr(os.path, 'ismount', lambda path: True)
    assert h.shared_cargo_target({'build_drives': [entry]}) == tmp_path / 'g' / 'cargo-target'


@pytest.mark.skipif(not os.path.isdir('/proc/self'), reason='needs a nested /proc filesystem')
def test_a_nested_filesystem_is_not_the_declared_mount(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 500.0)
    entry = {'path': '/proc', 'mount': '/', 'disk_floor_gb': 20}
    with pytest.raises(OSError, match='not mounted'):
        h.shared_cargo_target({'build_drives': [entry]})


def test_the_running_guard_stops_a_build_whose_launch_mount_went_away(monkeypatch, tmp_path):
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 400.0)
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 0)
    here = h.filesystem_id(tmp_path)
    assert h.running_disk_refusal(tmp_path, 'implement', tmp_path, here) is None
    reason = h.running_disk_refusal(tmp_path, 'implement', tmp_path, here + 1)
    assert reason == f'disk_low: the filesystem of {tmp_path} changed since launch'


def test_a_dotdot_path_is_not_inside_the_mount_it_names(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    (tmp_path / 'm').mkdir()
    (tmp_path / 'other').mkdir()
    monkeypatch.setattr(os.path, 'ismount', lambda path: True)
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 500.0)
    entry = {'path': str(tmp_path / 'm' / '..' / 'other'), 'mount': str(tmp_path / 'm'),
             'disk_floor_gb': 20}
    with pytest.raises(OSError, match='not mounted'):
        h.shared_cargo_target({'build_drives': [entry]})


def test_the_running_guard_monitors_the_target_the_run_was_launched_with(monkeypatch, tmp_path):
    launched, other = tmp_path / 'launched', tmp_path / 'other'
    free = {str(launched): 1.0, str(other): 400.0}
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: free.get(str(path), 400.0))
    monkeypatch.setattr(h, 'cargo_target_size_gb', lambda path: 0)
    reason = h.running_disk_refusal(tmp_path, 'implement', launched)
    assert reason == 'disk_low: free 1.00 GB, stop 2 GB'
    assert h.running_disk_refusal(tmp_path, 'implement', other) is None


def test_the_running_guard_keeps_the_target_below_the_admission_floor(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    (tmp_path / 'g').mkdir()
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 19.0)
    policy = {'build_drives': [{'path': str(tmp_path / 'g'), 'disk_floor_gb': 20}]}
    with pytest.raises(OSError, match='disk_low'):
        h._target_for(policy)
    assert h._target_for(policy, floors=False) == tmp_path / 'g' / 'cargo-target'


def test_an_extra_check_failure_on_one_drive_does_not_stop_a_healthy_one():
    def check(path):
        if path == '/bad':
            raise OSError('check failed')
    free = _free({'/bad': 30.0, '/ok': 20.0})
    assert source.select_drive([('/bad', 5.0), ('/ok', 5.0)], lambda: None, free,
                               check=check) == ('/ok', None)


def test_admission_resolves_the_target_from_the_checkouts_policy(monkeypatch, tmp_path):
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    (tmp_path / 'g').mkdir()
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 10.0)
    policy = {'build_drives': [{'path': str(tmp_path / 'g'), 'disk_floor_gb': 20}]}
    with pytest.raises(OSError, match='disk_low: no build drive meets its floor'):
        h._target_for(policy)
    monkeypatch.setattr(h, 'shared_cargo_target', lambda: tmp_path / 'default')
    assert h._target_for({}) == tmp_path / 'default'


def test_the_workstation_config_puts_the_cargo_target_on_g(monkeypatch):
    from pathlib import Path
    from plangraph.settings import _policy_data
    monkeypatch.delenv('PLANGRAPH_CARGO_TARGET_DIR', raising=False)
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'deploy/workstation/work-drives.json')
    monkeypatch.setattr(h, 'disk_free_gb', lambda path: 100.0)
    monkeypatch.setattr(Path, 'is_dir', lambda self: str(self).startswith('/mnt/work-g'))
    monkeypatch.setattr(os.path, 'ismount', lambda path: path == '/mnt/work-g')
    monkeypatch.setattr(source, 'same_device', lambda a, b: True)
    policy = _policy_data(Path(__file__).resolve().parents[1])
    assert str(h.shared_cargo_target(policy)) == '/mnt/work-g/plangraph/build/cargo-target'
