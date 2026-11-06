"""Read keeper-owned mirrored records without following untrusted links."""

import json
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from .evidence_runs import RUN_ID_RE, RunRecordAdapter
from .evidence_types import Result

MAX_RECORD_BYTES = 16 * 1024 * 1024


class SpawnerRecordAdapter:
    def __init__(self, directory, *, uid=None):
        self.directory = Path(directory) if directory else None
        self.uid = os.geteuid() if uid is None else uid

    def _trusted(self, fd, directory=False):
        info = os.fstat(fd)
        if info.st_uid != self.uid:
            raise ValueError("record component has wrong owner")
        if info.st_mode & 0o022:
            raise ValueError("record component is writable by group or other")
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("record component has wrong file type")
        if not directory and info.st_size > MAX_RECORD_BYTES:
            raise ValueError("record exceeds size limit")

    def record(self, host, run_id):
        if self.directory is None:
            raise ValueError("spawner_records_dir is not configured")
        if (not isinstance(host, str) or not RUN_ID_RE.fullmatch(host)
                or host in (".", "..")):
            raise ValueError("invalid spawner host")
        if not RunRecordAdapter(None).valid_run_id(run_id):
            raise ValueError("invalid spawner run id")
        root = self.directory.resolve()
        candidate = root / host / run_id / "record.json"
        if not candidate.resolve().is_relative_to(root):
            raise ValueError("record escapes mirror directory")
        with ExitStack() as stack:
            def opened(name, flags, parent=None):
                fd = os.open(name, flags, dir_fd=parent)
                stack.callback(os.close, fd)
                return fd

            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            parent = opened(root, flags)
            for name in (host, run_id):
                parent = opened(name, flags, parent)
                self._trusted(parent, directory=True)
            fd = opened("record.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, parent)
            self._trusted(fd)
            with os.fdopen(os.dup(fd), "rb") as handle:
                data = handle.read(MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError("record exceeds size limit")
        try:
            record = json.loads(data)
        except (ValueError, UnicodeError):
            raise ValueError("record is malformed JSON") from None
        if not isinstance(record, dict):
            raise ValueError("record is not a JSON object")
        return record

    def verify(self, item, ref):
        try:
            record = self.record(ref.get("host"), ref.get("run_id"))
        except FileNotFoundError:
            return Result(False, "spawner record is missing", ref)
        except ValueError as exc:
            return Result(False, str(exc), ref)
        except OSError:
            return Result(False, "spawner record is unreadable or symlinked", ref)
        leg = record.get("chosen_leg")
        writer = ref.get("writer")
        family = record.get("writer_family")
        checks = (
            (record.get("run_id") == ref.get("run_id"), "record run_id mismatch"),
            (record.get("host") == ref.get("host"), "record host mismatch"),
            (isinstance(record.get("item_key"), str) and record["item_key"] in
             [v for v in (item.get("key"), item.get("id")) if isinstance(v, str)],
             "record item_key missing or mismatched"),
            (record.get("status") == "completed", "record status is not completed"),
            (type(record.get("exit_code")) is int and record["exit_code"] == 0,
             "record exit_code is not integer zero"),
            (isinstance(writer, str) and bool(writer.strip()), "ref writer is required"),
            (isinstance(leg, dict) and leg.get("id") == writer, "record writer leg mismatch"),
            (isinstance(family, str) and bool(family.strip()), "record writer_family is empty"),
            (record.get("served_family") in ("unwitnessed", family),
             "record served_family mismatch"),
        )
        for passed, reason in checks:
            if not passed:
                return Result(False, reason, ref)
        return Result(True, f"run {ref['run_id']} on {ref['host']} for "
                      f"{item.get('key') or item.get('id')} rc=0 leg={writer} family={family}", ref)
