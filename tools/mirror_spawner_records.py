#!/usr/bin/env python3
"""Manually mirror terminal spawner proof, never harness output."""
import argparse
import errno
import fcntl
from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import tempfile

LIMIT = 16 * 1024 * 1024


def safe_name(value):
    return value not in ('.', '..') and re.fullmatch(r'[A-Za-z0-9._-]+', value)


def sources(args):
    if args.source:
        for directory in Path(args.source).iterdir():
            path = directory / 'record.json'
            if safe_name(directory.name) and not directory.is_symlink() and path.is_file() and not path.is_symlink():
                with path.open('rb') as stream:
                    yield directory.name, stream.read(LIMIT + 1)
    else:
        command = (f'cd {shlex.quote(args.remote_runs)} && '
                   'find . -mindepth 2 -maxdepth 2 -type f -name record.json -print0 | '
                   'tar --null -T - -cf -')
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '--', args.ssh, command],
                                  stdout=subprocess.PIPE, stderr=errors) as proc:
                try:
                    with tarfile.open(fileobj=proc.stdout, mode='r|*') as archive:
                        for member in archive:
                            name = member.name.removeprefix('./')
                            parts = name.split('/')
                            if member.isfile() and len(parts) == 2 and safe_name(parts[0]) and parts[1] == 'record.json':
                                with archive.extractfile(member) as stream:
                                    yield parts[0], stream.read(LIMIT + 1)
                    # Drain tar padding without retaining the remaining stream.
                    while proc.stdout.read(64 * 1024):
                        pass
                    code = proc.wait()
                    if code not in (0, 1):
                        errors.seek(max(0, errors.seek(0, os.SEEK_END) - 200))
                        raise subprocess.CalledProcessError(code, 'ssh', stderr=errors.read(200))
                finally:
                    proc.stdout.close()
                    if proc.poll() is None:
                        proc.kill()


def reduce_record(run_id, raw, host):
    if len(raw) > LIMIT:
        return None, 'oversize'
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None, 'json'
    if not isinstance(record, dict):
        return None, 'object'
    for key, expected in [('run_id', run_id), ('host', host)]:
        if record.get(key) != expected:
            return None, key
    # Status::is_terminal, rust/worker-core/src/run/record.rs: neither queued nor running.
    status = record.get('status')
    if not isinstance(status, str) or not re.fullmatch(r'[a-z_]{1,40}', status) or status in ('queued', 'running'):
        return None, 'status'
    def identifier(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', value)
    if not identifier(record.get('item_key')):
        return None, 'item_key'
    if record.get('exit_code') is not None and type(record['exit_code']) is not int:
        return None, 'exit_code'
    for key in ('writer_family', 'served_family'):
        if record.get(key) is not None and not identifier(record[key]):
            return None, key
    leg = record.get('chosen_leg')
    if leg is not None and (not isinstance(leg, dict) or (leg.get('id') is not None and not identifier(leg['id']))):
        return None, 'chosen_leg'
    reduced = {k: record.get(k) for k in ('run_id', 'host', 'status', 'exit_code', 'item_key', 'writer_family', 'served_family')}
    reduced['chosen_leg'] = {'id': leg.get('id')} if leg is not None else None
    return (json.dumps(reduced, sort_keys=True, ensure_ascii=True) + '\n').encode(), None


def write_record(dest, host, run_id, data, dry):
    # Directory FDs plus O_NOFOLLOW prevent symlink swaps, including during writes.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (Path(os.path.abspath(dest)).parts[1:] + (host, run_id)):
            if not dry:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if dry:
                    return 'mirrored'
                raise
            os.close(fd)
            fd = child
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            old = os.open('record.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            old = None
        if old is not None:
            with os.fdopen(old, 'rb') as stream:
                return 'unchanged' if stream.read(len(data) + 1) == data else 'conflicts'
        if not dry:
            # Hard-link publication never replaces a non-cooperating writer's proof.
            name = next(tempfile._get_candidate_names()) + '.tmp'
            temp = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            try:
                with os.fdopen(temp, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(name, 'record.json', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    old = os.open('record.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(old, 'rb') as stream:
                        return 'unchanged' if stream.read(len(data) + 1) == data else 'conflicts'
                os.fsync(fd)
            finally:
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return 'mirrored'
    except OSError as exc:
        return 'conflicts' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'io_errors'
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog='Exit 1 on conflicts (including symlink refusals), source failures, or source/destination OS errors (io_errors); otherwise exit 0.')
    parser.add_argument('--host', required=True)
    parser.add_argument('--dest', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--source')
    group.add_argument('--ssh')
    parser.add_argument('--remote-runs')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if args.ssh and args.ssh.startswith('-'):
        parser.error('--ssh alias must not start with -')
    if not safe_name(args.host) or bool(args.ssh) != bool(args.remote_runs):
        parser.error('safe host and --ssh with --remote-runs required')
    counts, skipped, failed = Counter(), Counter(), False
    try:
        # Finish source acquisition before any writes (notably failed SSH).
        with tempfile.TemporaryFile(mode='w+b') as records:
            for run_id, raw in sources(args):
                data, reason = reduce_record(run_id, raw, args.host)
                if reason:
                    skipped[reason] += 1
                else:
                    records.write(data)
                del raw
            records.seek(0)
            for data in records:
                run_id = json.loads(data)['run_id']
                counts[write_record(args.dest, args.host, run_id, data, args.dry_run)] += 1
    except (OSError, subprocess.CalledProcessError, tarfile.TarError):
        failed = True
        skipped['source'] += 1
    print(f"mirrored={counts['mirrored']} unchanged={counts['unchanged']} conflicts={counts['conflicts']} io_errors={counts['io_errors']} skipped={json.dumps(dict(skipped), sort_keys=True)}")
    return int(failed or counts['conflicts'] > 0 or counts['io_errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
