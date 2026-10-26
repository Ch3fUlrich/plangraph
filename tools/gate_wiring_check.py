'''Check CI gate wiring changes for Gate-Edit trailers.'''
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

try:  # Module import in tests; direct script execution in CI.
    from tools.ci_commit_gate import _trailer_block_lines, _touched_paths
except ModuleNotFoundError:
    from ci_commit_gate import _trailer_block_lines, _touched_paths


class GateError(Exception):
    '''An error that should terminate main with a specific code.'''

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class MissingJob(Exception):
    '''The requested job is absent from a workflow revision.'''

    def __init__(self, job: str, rev: str) -> None:
        super().__init__(f'{job} job missing at {rev}')
        self.job = job
        self.rev = rev


def _git_show(rev: str, workflow: str, cwd: str | Path | None) -> str:
    command = ['git', 'show', f'{rev}:{workflow}']
    try:
        result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or f'git show failed for {rev}:{workflow}'
        raise GateError(message, 2) from exc
    return result.stdout


def wiring_at(rev: str, workflow: str, job: str, cwd: str | Path | None = None) -> dict[str, object]:
    '''Return the wiring for job at rev, raising MissingJob when absent.'''
    text = _git_show(rev, workflow, cwd)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GateError(f'YAML parse error at {rev}: {exc}', 2) from exc
    if not isinstance(doc, dict):
        raise GateError(f'workflow at {rev} is not a mapping', 2)
    jobs = doc.get('jobs')
    if not isinstance(jobs, dict) or job not in jobs:
        raise MissingJob(job, rev)
    triggers = doc.get('on', doc.get(True))
    return {'on': triggers, 'job': jobs[job]}


def _git_out(args: list[str], cwd: str | Path | None) -> str:
    command = ['git', *args]
    try:
        result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or f'git command failed: {" ".join(command)}'
        raise GateError(message, 2) from exc
    return result.stdout


_GATE_EDIT = re.compile(r'Gate-Edit: D-\d+')

def _trailer_block(message: str) -> list[str]:
    '''Read only the final trailer block, using the commit gate parser.'''
    return _trailer_block_lines(message)

def _commit_shas(base: str, head: str, cwd: str | Path | None) -> list[str]:
    text = _git_out(['log', '--format=%H', f'{base}..{head}'], cwd)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _commit_message(sha: str, cwd: str | Path | None) -> str:
    return _git_out(['show', '-s', '--format=%B', sha], cwd)


def _commit_paths(sha: str, cwd: str | Path | None) -> list[str]:
    return _touched_paths(sha, cwd=cwd)


def has_gate_edit(base: str, head: str, cwd: str | Path | None = None) -> bool:
    '''Return True when any commit in base..head has a valid Gate-Edit trailer.'''
    for sha in _commit_shas(base, head, cwd):
        message = _commit_message(sha, cwd)
        if any(_GATE_EDIT.fullmatch(line) for line in _trailer_block(message)):
            return True
    return False


def commits_touching(base: str, head: str, workflow: str, cwd: str | Path | None = None) -> list[tuple[str, bool]]:
    '''Return (sha, has_gate_edit) for commits in base..head touching workflow.'''
    touching: list[tuple[str, bool]] = []
    for sha in _commit_shas(base, head, cwd):
        message = _commit_message(sha, cwd)
        paths = _commit_paths(sha, cwd)
        if workflow not in paths:
            continue
        has_edit = any(_GATE_EDIT.fullmatch(line) for line in _trailer_block(message))
        touching.append((sha, has_edit))
    return touching


def commits_missing_gate_edit(base: str, head: str, workflow: str, cwd: str | Path | None = None) -> list[str]:
    '''Return shas in base..head that touch workflow without a strict Gate-Edit line in their message.'''
    return [sha for sha, has_edit in commits_touching(base, head, workflow, cwd=cwd) if not has_edit]


def _wiring_diff(base_wiring: dict[str, object], head_wiring: dict[str, object]) -> str:
    base_text = json.dumps(base_wiring, indent=2, sort_keys=True)
    head_text = json.dumps(head_wiring, indent=2, sort_keys=True)
    lines = difflib.unified_diff(
        base_text.splitlines(),
        head_text.splitlines(),
        fromfile='base',
        tofile='head',
        lineterm='',
    )
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Check CI gate wiring changes for Gate-Edit trailers.')
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--workflow', default='.github/workflows/tests.yml')
    parser.add_argument('--job', default='ci-gate')
    args = parser.parse_args(argv)
    try:
        base_wiring = wiring_at(args.base, args.workflow, args.job)
    except MissingJob:
        print(f'{args.job} job missing at base', file=sys.stderr)
        return 2
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    try:
        head_wiring = wiring_at(args.head, args.workflow, args.job)
    except MissingJob:
        print(f'{args.job} job missing at head', file=sys.stderr)
        return 1
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if base_wiring == head_wiring:
        print('gate wiring unchanged')
        return 0
    print(_wiring_diff(base_wiring, head_wiring))
    try:
        missing = commits_missing_gate_edit(args.base, args.head, args.workflow)
        touching = commits_touching(args.base, args.head, args.workflow)
    except GateError as exc:
        print(exc.message, file=sys.stderr)
        return exc.code
    if not touching:
        print('gate wiring changed WITHOUT Gate-Edit', file=sys.stderr)
        return 1
    if not missing:
        print('gate wiring changed WITH Gate-Edit: review the diff')
        return 0
    print('gate wiring changed WITHOUT Gate-Edit: ' + ' '.join(missing), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
