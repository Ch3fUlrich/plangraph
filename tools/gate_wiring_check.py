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


def has_gate_edit(base: str, head: str, cwd: str | Path | None = None) -> bool:
    '''Return True when any commit in base..head has a valid Gate-Edit trailer.'''
    command = ['git', 'log', '--no-merges', '--format=%H%x00%B%x01', f'{base}..{head}']
    try:
        result = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or f'git log failed for {base}..{head}'
        raise GateError(message, 2) from exc
    pattern = re.compile(r'Gate-Edit: D-\d+')
    for record in result.stdout.split('\x01'):
        if not record.strip():
            continue
        parts = record.split('\x00', 1)
        if len(parts) != 2:
            continue
        for line in parts[1].splitlines():
            if pattern.fullmatch(line):
                return True
    return False


def commits_touching(base: str, head: str, workflow: str, cwd: str | Path | None = None) -> list[tuple[str, bool]]:
    '''Return (sha, has_gate_edit) for commits in base..head touching workflow.'''
    command = ['git', 'log', '--no-merges', '--name-only', '--format=%x01%H%x00%B%x00', f'{base}..{head}']
    try:
        completed = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise GateError(f'git log failed: {exc.stderr.strip()}', 2) from exc
    touching: list[tuple[str, bool]] = []
    for record in completed.stdout.split('\x01'):
        if not record:
            continue
        sha, sep, rest = record.partition('\x00')
        if not sep:
            continue
        message, sep, files = rest.partition('\x00')
        if not sep:
            continue
        paths = [line.strip() for line in files.splitlines() if line.strip()]
        if workflow not in paths:
            continue
        has_edit = any(re.fullmatch(r'Gate-Edit: D-\d+', line) for line in message.splitlines())
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
