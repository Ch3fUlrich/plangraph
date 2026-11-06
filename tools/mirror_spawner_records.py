#!/usr/bin/env python3
"""Manually mirror terminal spawner proof, never harness output."""
import argparse
import fcntl
from collections import Counter
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

LIMIT = 16 * 1024 * 1024
# Mirrors Status::is_terminal in rust/worker-core/src/run/record.rs (Status enum).
TERMINAL = {'completed', 'failed', 'cancelled', 'stalled', 'timeout', 'refused', 'limited', 'dry_run'}


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
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '--', args.ssh,
                                 'tar', '-C', args.remote_runs, '-cf', '-', '--wildcards',
                                 '*/record.json'], capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:*') as archive:
            for member in archive:
                parts = member.name.split('/')
                if member.isfile() and len(parts) == 2 and safe_name(parts[0]) and parts[1] == 'record.json':
                    with archive.extractfile(member) as stream:
                        yield parts[0], stream.read(LIMIT + 1)


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
    if not isinstance(record.get('status'), str) or record['status'] not in TERMINAL:
        return None, 'status'
    if not isinstance(record.get('item_key'), str) or not record['item_key']:
        return None, 'item_key'
    # Do not permit nested objects to smuggle output through scalar fields.
    reduced = {k: record.get(k) if isinstance(record.get(k), (str, int, float, bool, type(None))) else None
               for k in ('run_id', 'host', 'status', 'exit_code', 'item_key', 'writer_family', 'served_family')}
    leg = record.get('chosen_leg')
    reduced['chosen_leg'] = {'id': leg['id']} if isinstance(leg, dict) and isinstance(leg.get('id'), str) else None
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
    except OSError:
        return 'io_errors'
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--dest', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--source')
    group.add_argument('--ssh')
    parser.add_argument('--remote-runs')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not safe_name(args.host) or bool(args.ssh) != bool(args.remote_runs):
        parser.error('safe host and --ssh with --remote-runs required')
    counts, skipped, failed = Counter(), Counter(), False
    try:
        # Finish source acquisition before any writes (notably failed SSH).
        records = list(sources(args))
        for run_id, raw in records:
            data, reason = reduce_record(run_id, raw, args.host)
            if reason:
                skipped[reason] += 1
            else:
                counts[write_record(args.dest, args.host, run_id, data, args.dry_run)] += 1
    except (OSError, subprocess.CalledProcessError, tarfile.TarError):
        failed = True
        skipped['source'] += 1
    print(f"mirrored={counts['mirrored']} unchanged={counts['unchanged']} conflicts={counts['conflicts']} io_errors={counts['io_errors']} skipped={json.dumps(dict(skipped), sort_keys=True)}")
    return int(failed or counts['conflicts'] > 0 or counts['io_errors'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
