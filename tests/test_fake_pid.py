"""Host-independent fake children and a guard against literal PIDs."""
import ast
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_pid import FAKE_PID
from plangraph.spawner import reap
from plangraph.spawner.runner import JobInfo, SpawnerRunner

ALLOW = {
    ('test_dispatch_refusal_window.py', 'test_candidate_at_exit_books_and_reaps_the_process_tree.run'):
        'Injected _reap only records the PID; no process lookup.',
    ('test_spawner_kill_r3.py', '_Child'):
        '_nt_table and _nt_alive patched to synthetic state in every caller.',
    ('test_spawner_kill_own_tree.py', '_nt_runner.factory'):
        '_Fake(4242): _nt_alive and _nt_table patched to synthetic state.',
    ('test_spawner_kill_own_tree.py', 'test_unproven_ownership_never_reaches_killer.run'):
        '_Fake(4242): reap.alive patched; ownership refusal must not call killer.',
    ('test_spawner_kill_own_tree.py', 'test_unproven_pid_timeout_ends_kill_incomplete_without_blocking.run'):
        'Hanging(4242): reap.alive patched; injected killer only records PID.',
    ('test_mcp_supervise_proc.py', 'TestChildIsolation.test_children_start_in_their_own_session'):
        'Popen and write_pid_file patched; PID is bookkeeping only.',
    ('test_mcp_supervise_recover.py', 'TestProjectDirIsEnforced.test_existing_project_dir_becomes_the_cwd'):
        'Popen and write_pid_file patched; PID is bookkeeping only.',
    ('test_mcp_supervise_recover.py', 'TestProxyResolvedOnce.test_version_probe_runs_once_not_per_restart'):
        'Popen and write_pid_file patched; PID is bookkeeping only.',
    ('test_spawn_cli.py', 'test_windows_killer.run'):
        'taskkill uses injected command_factory and synthetic members only.',
    ('test_mcp_supervise_recover.py', 'TestKillResult._taskkill'):
        'subprocess.run and pid_alive patched; no host process operation.',
}


def offenders(source):
    found = []
    def walk(node, scope=(), in_class=False):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
            in_class = isinstance(node, ast.ClassDef)
        values = []
        def pid_target(target):
            return (isinstance(target, ast.Name) and target.id == 'pid' and in_class
                    or isinstance(target, ast.Attribute) and target.attr == 'pid')
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if pid_target(target):
                    values.append(node.value)
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    values.extend(v for t, v in zip(target.elts, node.value.elts)
                                  if pid_target(t))
        if isinstance(node, ast.AnnAssign):
            if pid_target(node.target):
                values.append(node.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            values.extend(v for a, v in zip((*args.posonlyargs, *args.args)[-len(args.defaults):], args.defaults)
                          if a.arg == 'pid')
            values.extend(v for a, v in zip(args.kwonlyargs, args.kw_defaults) if a.arg == 'pid')
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == 'type' and len(node.args) == 3 and isinstance(node.args[2], ast.Dict):
                values.extend(v for k, v in zip(node.args[2].keys, node.args[2].values)
                              if isinstance(k, ast.Constant) and k.value == 'pid')
            if node.func.id in {'_Fake', 'Hanging', 'kill_process_tree'} and node.args:
                values.append(node.args[0])
        if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == 'SimpleNamespace'
                or isinstance(node.func, ast.Attribute) and node.func.attr == 'SimpleNamespace'):
            values.extend(k.value for k in node.keywords if k.arg == 'pid')
        if any(isinstance(v, ast.Constant) and isinstance(v.value, int)
               and v.value < FAKE_PID for v in values):
            found.append('.'.join(scope))
        for child in ast.iter_child_nodes(node):
            walk(child, scope, in_class)
    walk(ast.parse(source))
    return found


def test_no_small_fake_pid_literals():
    found = {(p.name, scope) for p in Path(__file__).parent.rglob('*.py')
             for scope in offenders(p.read_text())}
    assert found == set(ALLOW), (found - set(ALLOW), set(ALLOW) - found)
    assert all(ALLOW.values())


@pytest.mark.parametrize('source', [
    'class Child:\n pid = 1234',
    'class Child:\n pid: int = 1234',
    'class Child:\n pid, returncode = 1234, 0',
    'proc = SimpleNamespace(pid=1234)',
    'proc = types.SimpleNamespace(pid=1234)',
    'class C:\n def __init__(self):\n  self.pid = 1234',
    'def __init__(self, pid=1234): pass',
    'def f(*, pid=1234): pass',
    'self.pid: int = 1234',
    'self.pid, self.returncode = 1234, 0',
    "proc = type('P', (), {'pid': 1234})()",
])
def test_guard_catches_new_offender(source):
    assert offenders(source)


@pytest.mark.parametrize('name', ['gateway_witness', 'w36_regressions', 'writer_isolation'])
def test_stub_comment_describes_impossible_pid(name):
    source = Path(__file__).with_name('test_' + name + '.py').read_text()
    assert 'impossible pid' in next(line for line in source.splitlines() if line.startswith('pytestmark'))


@pytest.mark.parametrize('old_pid', [1234, 999])
def test_live_old_pid_fails_but_fake_pid_completes(monkeypatch, old_pid):
    # Shared reap path of all four reported tests; no fixture rewrites needed.
    with subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) as child:
        try:
            real_kill = os.kill
            def collision(pid, sig):
                if pid == old_pid:
                    return real_kill(child.pid, sig)
                if pid >= FAKE_PID:
                    raise ProcessLookupError
                raise AssertionError('unexpected host PID probe')
            monkeypatch.setattr(os, 'kill', collision)
            monkeypatch.setattr(reap, 'alive', lambda pid, *a, **k: pid == old_pid)
            monkeypatch.setattr(reap, 'group_members', lambda pid: [old_pid] if pid == old_pid else [])
            async def finish(pid):
                runner = SpawnerRunner()
                job = runner.jobs['j'] = JobInfo('j', 'completed', '', '')
                await runner._reap_process('j', SimpleNamespace(pid=pid, returncode=0))
                return job
            os.kill(old_pid, 0)
            old = asyncio.run(finish(old_pid))
            assert old.status == 'failed' and old.survivors == [old_pid]
            assert asyncio.run(finish(FAKE_PID)).status == 'completed'
        finally:
            real_kill(child.pid, 15)
            child.wait()
