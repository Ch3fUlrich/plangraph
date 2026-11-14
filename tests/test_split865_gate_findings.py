"""Regression coverage for the split review's local gate findings."""
import importlib.util
from pathlib import Path
import subprocess

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_strips_complete_config_namespace(tmp_path):
    module = load('preflight')
    env = {'PATH': __import__('os').environ['PATH'], 'GIT_CONFIG_GLOBAL': '/caller',
           'GIT_CONFIG_SYSTEM': '/caller', 'GIT_CONFIG_FUTURE': 'caller', 'GIT_PREFIX': 'caller'}
    clean = module.test_environment(tmp_path, env)
    assert not any(k.startswith('GIT_CONFIG') or k == 'GIT_PREFIX' for k in clean)


def test_shared_control_keeps_all_named_consumers():
    module = load('affected_tests')
    consumers = {f'tests/test_consumer_{n}.py': '# conftest.py' for n in range(40)}
    assert set(consumers) <= module.local_control_tests(consumers, 'tests/conftest.py')


def test_policy_main_baseline_covers_inherited_stack(tmp_path):
    module = load('preflight')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'base', '--allow-empty'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True)
    assert module.policy_baseline(tmp_path, 'HEAD') == 'origin/main'


@pytest.fixture
def rust_selection(tmp_path, monkeypatch):
    module = load('preflight')
    rust = tmp_path / 'rust'
    for name, data in {
        'one': '[package]\nname = "one"\n',
        'worker': '[workspace]\n[package]\nname = "worker"\n'
                  '[dependencies]\none = { path = "../one" }\n',
        'spikes/temporal-spike': '[workspace]\n[package]\nname = "temporal-spike"\n',
    }.items():
        (rust / name).mkdir(parents=True)
        (rust / name / 'Cargo.toml').write_text(data)
    (rust / 'Cargo.toml').write_text('[workspace]\nmembers = ["one"]\n')
    monkeypatch.setattr(module.shutil, 'which', lambda *a, **k: '/fake/cargo')

    def select(*paths, relative=False):
        monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: '\n'.join(paths))
        monkeypatch.chdir(tmp_path)
        return module.rust_commands(Path('.') if relative else tmp_path, 'base', 'head', {})

    return rust, select


def standalone_checks(manifest):
    target = ['--manifest-path', str(manifest)]
    return [['cargo', 'fmt', *target, '--check'],
            ['cargo', 'clippy', *target, '--all-targets', '--', '-D', 'warnings'],
            ['cargo', 'test', *target, '--lib', '--bins']]


def test_standalone_crate_is_checked(rust_selection):
    rust, select = rust_selection
    assert select('rust/worker/src/lib.rs') == standalone_checks(rust / 'worker/Cargo.toml')


@pytest.mark.parametrize('section', ['dependencies', 'dev-dependencies', 'build-dependencies',
                                      "target.'cfg(unix)'.dependencies"])
def test_standalone_path_dependents_are_checked(rust_selection, section):
    rust, select = rust_selection
    manifest = rust / 'worker/Cargo.toml'
    manifest.write_text('[workspace]\n[package]\nname = "worker"\n'
                        f'[{section}]\nrenamed = {{ path = "../one", package = "one" }}\n')
    commands = select('rust/one/src/lib.rs')
    assert commands == [['cargo', 'fmt', '--check'],
                        ['cargo', 'clippy', '-p', 'one', '--all-targets', '--', '-D', 'warnings'],
                        ['cargo', 'test', '-p', 'one'], *standalone_checks(manifest)]


def test_transitive_dependents_across_workspaces(rust_selection):
    rust, select = rust_selection
    (rust / 'two').mkdir()
    (rust / 'two/Cargo.toml').write_text('[package]\nname = "two"\n')
    (rust / 'Cargo.toml').write_text('[workspace]\nmembers = ["one", "two"]\n'
                                    '[workspace.dependencies]\ntwo = { path = "two" }\n')
    with (rust / 'one/Cargo.toml').open('a') as handle:
        handle.write('[dependencies]\ntwo = { workspace = true }\n')
    commands = select('rust/two/src/lib.rs')
    assert commands[2] == ['cargo', 'test', '-p', 'one', '-p', 'two']
    assert commands[3:] == standalone_checks(rust / 'worker/Cargo.toml')


def test_shared_workspace_changes_skip_unrelated_spike(rust_selection):
    rust, select = rust_selection
    commands = select('rust/Cargo.toml')
    assert commands[2] == ['cargo', 'test', '-p', 'one']
    assert commands[3:] == standalone_checks(rust / 'worker/Cargo.toml')


def test_spike_uses_ci_replay_check(rust_selection):
    rust, select = rust_selection
    assert select('rust/spikes/temporal-spike/tests/replay.rs') == [
        ['cargo', 'test', '--manifest-path', str(rust / 'spikes/temporal-spike/Cargo.toml'),
         '--test', 'replay']]


def test_relative_root_resolves_standalone_manifest(rust_selection):
    rust, select = rust_selection
    assert select('rust/worker/src/lib.rs', relative=True) == standalone_checks(rust / 'worker/Cargo.toml')
