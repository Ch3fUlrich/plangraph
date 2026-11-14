"""Regression coverage for the split review's local gate findings."""
import importlib.util
from pathlib import Path
import subprocess


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
