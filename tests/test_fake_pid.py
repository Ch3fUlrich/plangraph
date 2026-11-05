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
}
# Other retained literals: kill_own_tree._Fake/Hanging(4242) patch reap.alive
# or _nt_alive; mcp_supervise_{proc,recover} patch write_pid_file (bookkeeping);
# spawn_cli taskkill literals and kill_own_tree NT tables use command_factory
# and synthetic members/tables, not real processes.


def offenders(source):
    found = []
    def walk(node, scope=(), in_class=False):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, node.name)
            in_class = isinstance(node, ast.ClassDef)
        values = []
        if in_class and isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'pid':
                    values.append(node.value)
                if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                    values.extend(v for t, v in zip(target.elts, node.value.elts)
                                  if isinstance(t, ast.Name) and t.id == 'pid')
        if in_class and isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == 'pid':
                values.append(node.value)
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
])
def test_guard_catches_new_offender(source):
    assert offenders(source)


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
