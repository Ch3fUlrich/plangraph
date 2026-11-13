"""Regressions for the post-merge pilot safety findings."""
import os
from contextlib import contextmanager
from pathlib import Path
import re
import subprocess

import pytest
from test_pilot_preflight import ROOT, SECRET, env, pilot, run


@pytest.mark.parametrize('name', ['PG_TOKEN_FILE', 'PG_CARD_ALLOWLIST', 'PG_JAIL_BIN'])
def test_symlink_inputs_refused(env, monkeypatch, capsys, name):
    target = env / 'token' if name == 'PG_TOKEN_FILE' else Path(os.environ[name])
    if name == 'PG_TOKEN_FILE':
        target.write_text(SECRET)
        target.chmod(0o600)
    link = env / 'linked-input'
    link.symlink_to(target)
    monkeypatch.setenv(name, str(link))
    code, output = run(capsys, 'preflight')
    assert code == 2
    assert f'REFUSE {"PG_KEEPER_TOKEN" if name == "PG_TOKEN_FILE" else name} ' in output
    assert len(output.splitlines()) == len(pilot.REQUIRED_ENV)


def test_non_utf8_token_preserves_per_name_output(env, monkeypatch, capsys):
    token = env / 'token'
    token.write_bytes(bytes([255]))
    token.chmod(0o600)
    monkeypatch.setenv('PG_TOKEN_FILE', str(token))
    code, output = run(capsys, 'preflight')
    assert code == 2
    assert 'REFUSE PG_KEEPER_TOKEN ' in output
    assert len(output.splitlines()) == len(pilot.REQUIRED_ENV)


@pytest.mark.parametrize('name', ['PG_TOKEN_FILE', 'PG_CARD_ALLOWLIST'])
def test_link_swap_after_metadata_check(env, monkeypatch, capsys, name):
    target = env / 'token' if name == 'PG_TOKEN_FILE' else Path(os.environ[name])
    if name == 'PG_TOKEN_FILE':
        target.write_text(SECRET)
        target.chmod(0o600)
        monkeypatch.setenv(name, str(target))
    original = pilot.absolute_file
    def absolute_file(value):
        good = original(value)
        if value == str(target):
            moved = target.with_suffix('.moved')
            target.rename(moved)
            target.symlink_to(moved)
        return good
    monkeypatch.setattr(pilot, 'absolute_file', absolute_file)
    code, output = run(capsys, 'preflight')
    assert code == 2
    assert len(output.splitlines()) == len(pilot.REQUIRED_ENV)


@pytest.mark.parametrize('value', [None, 'other'])
def test_jail_operand_matches_worker(env, monkeypatch, capsys, value):
    monkeypatch.setattr(pilot, 'persistent', lambda p: True)
    cli = os.environ['PG_JAIL_BIN']
    repo = env / 'repo'
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    if value is None:
        monkeypatch.delenv('PG_JAIL_BIN')
    else:
        other = env / value
        other.write_bytes(Path(cli).read_bytes())
        other.chmod(0o755)
        monkeypatch.setenv('PG_JAIL_BIN', str(other))
    code, output = run(capsys, 'verify', '--temporal-db', str(env / 'db'),
                       '--jail-bin', cli, '--repo', str(repo))
    assert code == 2
    assert 'REFUSE jail ' in output
    assert 'SKIP checks not run: jail failed' in output


@pytest.mark.parametrize('failure', ['init', 'add', 'commit'])
@pytest.mark.parametrize('existing', [False, True])
def test_setup_git_failure_is_retryable(env, monkeypatch, capsys, failure, existing):
    repo = env / 'repo'
    if existing:
        repo.mkdir()
    original = pilot.command
    def command(argv, **kwargs):
        result = original(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 1) if failure in argv else result
    with monkeypatch.context() as patch:
        patch.setattr(pilot, 'command', command)
        assert run(capsys, 'setup-repo', str(repo))[0] == 2
    assert not repo.exists() or not list(repo.iterdir())
    assert not (env / 'repo.cards.allow').exists()
    assert run(capsys, 'setup-repo', str(repo))[0] == 0


@pytest.mark.parametrize('failure', ['open', 'write', 'publish', 'unlink', 'rename'])
@pytest.mark.parametrize('existing', [False, True])
def test_setup_allowlist_failure_is_retryable(env, monkeypatch, capsys, failure, existing):
    repo = env / 'repo'
    allow = env / 'repo.cards.allow'
    if existing:
        repo.mkdir()
    original_open, original_link, original_rename = Path.open, os.link, Path.rename
    original_unlink = Path.unlink
    @contextmanager
    def open_file(path, *args, **kwargs):
        if path.name == allow.name and failure == 'open':
            raise OSError('allowlist unavailable')
        with original_open(path, *args, **kwargs) as stream:
            if path.name == allow.name and failure == 'write':
                stream.write('partial')
                raise OSError('allowlist write failed')
            yield stream
    def publish(source, destination, *args, **kwargs):
        if destination == allow and failure == 'publish':
            raise OSError('allowlist publication failed')
        return original_link(source, destination, *args, **kwargs)
    def rename(source, destination):
        if destination == repo and failure == 'rename':
            raise OSError('repository publication failed')
        return original_rename(source, destination)
    def unlink(path, *args, **kwargs):
        if path.name == allow.name and path != allow and failure == 'unlink':
            raise OSError('staged allowlist cleanup failed')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', open_file)
        patch.setattr(os, 'link', publish)
        patch.setattr(Path, 'rename', rename)
        patch.setattr(Path, 'unlink', unlink)
        code, output = run(capsys, 'setup-repo', str(repo))
        assert code == 2
        assert 'REFUSE setup-repo ' in output
    assert not repo.exists() or not list(repo.iterdir())
    assert not os.path.lexists(allow)
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    assert allow.read_text().splitlines() == ['pilot:PILOT-1', 'pilot:PILOT-2']
    assert not list(env.glob('.pilot-repo-*'))


def test_setup_preserves_concurrent_allowlist(env, monkeypatch, capsys):
    repo = env / 'repo'
    allow = env / 'repo.cards.allow'
    original = os.link
    def publish(source, destination):
        allow.write_text('pilot:PILOT-9\n')
        return original(source, destination)
    monkeypatch.setattr(os, 'link', publish)
    assert run(capsys, 'setup-repo', str(repo))[0] == 2
    assert not repo.exists()
    assert allow.read_text() == 'pilot:PILOT-9\n'


@pytest.mark.parametrize('operation', ['unlink', 'rename'])
def test_setup_interrupt_after_link_is_retryable(env, monkeypatch, capsys, operation):
    repo, allow = env / 'repo', env / 'repo.cards.allow'
    original = getattr(Path, operation)
    def interrupt(path, *args, **kwargs):
        if (operation == 'unlink' and path.name == allow.name and path != allow
                or operation == 'rename' and args[0] == repo):
            raise KeyboardInterrupt('publication interrupted')
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, operation, interrupt)
        with pytest.raises(KeyboardInterrupt, match='publication interrupted'):
            pilot.setup_repo(str(repo))
    assert not os.path.lexists(allow)
    assert not repo.exists()
    assert run(capsys, 'setup-repo', str(repo))[0] == 0


@pytest.mark.parametrize('existing', [False, True])
def test_setup_recovers_orphan_allowlist(env, capsys, existing):
    repo, allow = env / 'repo', env / 'repo.cards.allow'
    if existing:
        repo.mkdir()
    allow.write_text('pilot:PILOT-1\npilot:PILOT-2\n')
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    assert (repo / '.git').is_dir()
    assert allow.read_text() == 'pilot:PILOT-1\npilot:PILOT-2\n'
    assert not list(env.glob('.pilot-repo-*'))


def test_setup_active_publication_cannot_be_reclaimed(env, monkeypatch, capsys):
    repo = env / 'repo'
    original = os.link
    def publish(source, destination):
        original(source, destination)
        assert run(capsys, 'setup-repo', str(repo))[0] == 2
        assert destination.read_text() == source.read_text()
    monkeypatch.setattr(os, 'link', publish)
    assert run(capsys, 'setup-repo', str(repo))[0] == 0
    assert (repo / '.git').is_dir()


def test_setup_cleanup_failure_leaves_recoverable_allowlist(env, monkeypatch, capsys):
    repo, allow = env / 'repo', env / 'repo.cards.allow'
    original_rename, original_unlink = Path.rename, Path.unlink
    def rename(path, destination):
        if destination == repo:
            raise RuntimeError('repository publication failed')
        return original_rename(path, destination)
    def unlink(path, *args, **kwargs):
        if path == allow:
            raise OSError('cleanup unavailable')
        return original_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', rename)
        patch.setattr(Path, 'unlink', unlink)
        with pytest.raises(RuntimeError, match='repository publication failed'):
            pilot.setup_repo(str(repo))
    assert allow.is_file() and not repo.exists()
    assert run(capsys, 'setup-repo', str(repo))[0] == 0


def test_setup_hardlink_requirement_documented_and_retryable(env, monkeypatch, capsys):
    for name in ['CONTRIBUTING.md', 'docs/plan/rtp1-live-run.md']:
        assert 'hard links' in (ROOT / name).read_text()
    def unsupported(*args, **kwargs):
        raise OSError('hard links unsupported')
    repo, allow = env / 'repo', env / 'repo.cards.allow'
    with monkeypatch.context() as patch:
        patch.setattr(os, 'link', unsupported)
        code, output = run(capsys, 'setup-repo', str(repo))
        assert code == 2 and 'REFUSE setup-repo ' in output
    assert not repo.exists() and not os.path.lexists(allow)
    assert run(capsys, 'setup-repo', str(repo))[0] == 0


@pytest.mark.parametrize('kind', ['alias', 'overmount', 'resolve-error'])
def test_mount_resolution_and_order(env, monkeypatch, kind):
    db = env / 'db'
    db.touch()
    alias = env / 'mount-alias'
    alias.symlink_to(env, target_is_directory=True)
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == Path('/tmp'):
            return env / 'other-tmp'
        if kind == 'resolve-error' and path == alias:
            raise OSError('mount unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    text = f'1 0 8:1 / {alias if kind != "overmount" else env} rw - xfs device rw\n'
    if kind == 'overmount':
        text += f'2 0 8:2 / {env} rw - nfs device rw\n'
    assert pilot.persistent(db, mountinfo=text) is (kind == 'alias')


@pytest.mark.parametrize('name', pilot.REQUIRED_ENV)
def test_name_parity_requires_consumption(monkeypatch, name):
    original = Path.read_text
    def read_text(path, *args, **kwargs):
        text = original(path, *args, **kwargs).replace(name, 'RENAMED_SETTING')
        return text + f'\n# {name}\n// {name}\nUNUSED = "{name}"\n'
    monkeypatch.setattr(Path, 'read_text', read_text)
    with pytest.raises(AssertionError, match=name):
        assert_name_parity()


def assert_name_parity():
    sources = ['rust/worker/src/bin/worker.rs', 'rust/worker/src/harness.rs',
               'rust/worker/src/buildid.rs', 'rust/worker-core/src/card_allowlist.rs',
               'src/plangraph/inbox/temporal_signal.py']
    if not all((ROOT / p).exists() for p in sources):
        pytest.skip('Rust worker sources absent')
    text = '\n'.join(line for p in sources for line in (ROOT / p).read_text().splitlines()
                     if not line.lstrip().startswith(('#', '//')))
    for name in pilot.REQUIRED_ENV:
        operands = [f'"{name}"']
        operands += re.findall(r'(\w+)\s*(?::[^=\n]+)?=\s*"' + name + '"', text)
        access = r'(?:env::var(?:_os)?|os\.environ\.get|env_or|required|lookup)\(\s*'
        assert any(re.search(access + r'(?:\w+::)*' + re.escape(arg) + r'\s*[,)]', text)
                   for arg in operands), name


def test_pilot_s3_documentation_evidence():
    changelog = (ROOT / 'changelog.d/TEMPORAL-PILOT-B-S3.md').read_text()
    assert 'refresh the worker lockfile' not in changelog
    jobs = (ROOT / 'tools/dell_ci_jobs.py').read_text().split('"rust-worker":', 1)[1]
    jobs = jobs.split('"rust-spike":', 1)[0]
    assert all(command in jobs for command in
               ['temporal server start-dev', 'cargo fmt --check',
                'cargo clippy --all-targets -- -D warnings', 'cargo test'])


def test_symlink_negative_control_documented():
    source = (ROOT / 'rust/worker/src/attempt.rs').read_text()
    comment = source.split('fn verify_refuses_a_worktree_whose_dot_git_is_a_symlink')[0]
    assert 'The `.git` rule prevents redirecting a healthy worktree via a symlink.' in comment[-600:]
    assert 'a test that runs without the symlink rule fails here' in comment[-600:]


@pytest.mark.parametrize('vector', ['malformed', 'non-utf8'])
def test_refusal_vectors_reject_success_exit(env, monkeypatch, capsys, vector):
    from test_pilot_preflight import test_malformed_values, test_non_utf8_allowlist
    original = pilot.main
    def success_exit(argv):
        original(argv)
        return 0
    monkeypatch.setattr(pilot, 'main', success_exit)
    with pytest.raises(AssertionError, match=r'assert 0 == 2'):
        if vector == 'malformed':
            test_malformed_values(env, monkeypatch, capsys, 'PG_BUILD_ID', 'bad build')
        else:
            test_non_utf8_allowlist(env, capsys)


@pytest.mark.parametrize('mutation', ['chmod', 'symlink'])
def test_cleanup_vector_detects_retained_home(env, monkeypatch, mutation):
    from test_pilot_preflight import test_child_home_cleanup
    homes = []
    original = pilot.tempfile.mkdtemp
    def mkdtemp(**kwargs):
        home = original(**kwargs)
        homes.append(home)
        return home
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pilot.tempfile, 'mkdtemp', mkdtemp)
            patch.setattr(pilot.shutil, 'rmtree', lambda *a, **kw: None)
            patch.setattr(pilot.os, 'unlink', lambda *a, **kw: None)
            with pytest.raises(AssertionError, match=r'assert not True\b[\s\S]*lexists'):
                test_child_home_cleanup(env, mutation)
    finally:
        for home in homes:
            if os.path.islink(home):
                os.unlink(home)
            else:
                os.chmod(home, 0o700)
                pilot.shutil.rmtree(home)


@pytest.mark.parametrize('vector', ['malformed', 'non-utf8'])
def test_refusal_meta_test_rejects_unrelated_assertion(env, monkeypatch, capsys, vector):
    import test_pilot_preflight as vectors
    def unrelated(*args):
        raise AssertionError('unrelated setup failure')
    name = 'test_malformed_values' if vector == 'malformed' else 'test_non_utf8_allowlist'
    monkeypatch.setattr(vectors, name, unrelated)
    with pytest.raises(AssertionError, match='Regex pattern did not match'):
        test_refusal_vectors_reject_success_exit(env, monkeypatch, capsys, vector)


@pytest.mark.parametrize('mutation', ['chmod', 'symlink'])
def test_cleanup_meta_test_rejects_unrelated_returncode(env, monkeypatch, mutation):
    monkeypatch.setattr(pilot, 'command', lambda *a, **kw:
                        subprocess.CompletedProcess([], 0, stdout=b''))
    with pytest.raises(AssertionError, match='Regex pattern did not match'):
        test_cleanup_vector_detects_retained_home(env, monkeypatch, mutation)
