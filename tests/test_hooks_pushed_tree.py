"""Pushed-tree gates use disposable repositories, never the writer checkout."""
import os
import hashlib
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / 'tools/hooks/pre-push'


def test_file_size_uses_each_pushed_tree(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               HOME=str(tmp_path), PREFLIGHT_SKIP='1')

    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, env=env, text=True).strip()

    def commit(text):
        (tmp_path / 'app.py').write_text(text)
        git('add', '.')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-qm', 'fixture')
        return git('rev-parse', 'HEAD')

    git('init', '-q')
    clean = commit('pass\n')
    large = commit('pass\n' * 801)

    def push(head):
        return subprocess.run([sys.executable, str(HOOK)], cwd=tmp_path, env=env,
                              input=f'refs/heads/test {head} refs/heads/test {"0" * 40}\n',
                              text=True, capture_output=True)

    result = push(clean)
    assert result.returncode == 0, result.stdout + result.stderr
    git('checkout', '--detach', clean)
    result = push(large)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'file-size gate failed' in result.stderr
    assert len(git('worktree', 'list', '--porcelain').split('worktree ')) == 2


def test_scrub_policy_comes_from_pushed_tree(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
               HOME=str(tmp_path), PREFLIGHT_SKIP='1')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, env=env, text=True).strip()
    git('init', '-q')
    address = '.'.join(['10', '20', '30', '40'])
    (tmp_path / 'app.py').write_text(f'address = "{address}"\n')
    rules = tmp_path / 'tools/split/scrub_ci.toml'
    rules.parent.mkdir(parents=True)
    digest = hashlib.sha256(address.encode()).hexdigest()
    rules.write_text('[scrub]\n[[scrub.allow_value]]\npath = "app.py"\nrule = "ipv4"\n'
                     f'value_sha256 = "{digest}"\nwhy = "synthetic fixture"\n')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'allowed')
    head = git('rev-parse', 'HEAD')
    rules.write_text('[scrub]\n')  # Pushing checkout policy disagrees with tip.
    result = subprocess.run([sys.executable, str(HOOK)], cwd=tmp_path, env=env,
                            input=f'refs/heads/test {head} refs/heads/test {"0" * 40}\n',
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_snapshot_preflight_keeps_selected_python(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', HOME=str(tmp_path))
    env.pop('PREFLIGHT_SKIP', None)
    env.pop('PREFLIGHT_PYTHON', None)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, env=env, text=True).strip()
    git('init', '-q')
    script = tmp_path / 'tools/preflight.py'
    script.parent.mkdir()
    script.write_text('import os, sys\nassert os.environ.get("PREFLIGHT_PYTHON") == sys.executable\n')
    git('add', '.')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
    head = git('rev-parse', 'HEAD')
    result = subprocess.run([sys.executable, str(HOOK)], cwd=tmp_path, env=env,
                            input=f'refs/heads/test {head} refs/heads/test {"0" * 40}\n',
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
