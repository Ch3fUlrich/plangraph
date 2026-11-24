"""Read keeper-owned spawner records without following untrusted links."""

import json
import os
import re
import socket
import stat
from contextlib import ExitStack
from pathlib import Path

from .evidence_runs import RUN_ID_RE, RunRecordAdapter
from .evidence_types import Result

MAX_RECORD_BYTES = 16 * 1024 * 1024


def _passing_review(text, *, require_explicit_accept=False):
    from ..review.verdict import _FIRST_WORD, _VERDICT_LINE, parse_verdict
    if not isinstance(text, str) or parse_verdict(text) != "pass":
        return False
    lines = text.splitlines()
    verdicts = [(i, m[1].strip().lower()) for i, line in enumerate(lines)
                if (m := _VERDICT_LINE.match(line))]
    if require_explicit_accept and verdicts:
        word = _FIRST_WORD.match(verdicts[-1][1].lstrip("*_`"))
        if word is None or word[1] not in ("pass", "accept"):
            return False
    gaming = re.compile(r"^TEST[_-]GAMING:[ \t]*(.*)$", re.I)
    answers = [m[1].strip().lower() for line in lines if (m := gaming.match(line))]
    return bool(verdicts and len({v for _, v in verdicts}) == 1 and len(answers) == 1
                and re.fullmatch(r"no(?:\s+-\s+[^|]+)?", answers[-1])
                and all(not line.strip() or gaming.match(line)
                        for line in lines[verdicts[-1][0] + 1:]))


def _required_seat(record):
    """D-6652 (rule 3.66 (f)): a record of one of the two required OpenCode seats must name files it read."""
    from ..planner.legs import REVIEW_REQUIRED
    leg = record.get("chosen_leg")
    return isinstance(leg, dict) and leg.get("id") in REVIEW_REQUIRED


def _tests_reported(text):
    """True when the last standalone ``TESTS:`` line of a review reports tests that ran."""
    fence, body = None, None
    for raw in (text or "").splitlines():
        line = raw.lstrip(" ")
        if marker := re.match(r"(`{3,}|~{3,})", line):
            fence = None if fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) else fence or marker[1]
        elif fence is None and line.startswith("TESTS:"):
            body = line[6:].strip().lower()
    return bool(body) and not re.search(r"requires approval|not permitted|denied|disallowed", body) \
        and not re.match(r"(?:none|no|not|n/a|skipped|could not|cannot|can not|did not)\b", body)


def _recorded_harness_seat(record, completion, text, head):
    """The countable seat of an unwitnessed run (rule 3.26), or ``None``.

    D-6762: the verdict names the exact head, FILES READ names a file, the record carries a
    harness and the chosen leg's family, and an opencode run reports tests that ran.
    """
    from ..review.family import model_family
    from ..review.verdict import has_files_read
    leg, harness = record.get("chosen_leg"), record.get("harness")
    model = leg.get("model") if isinstance(leg, dict) else None
    if (not isinstance(harness, str) or harness in ("", "unknown") or not isinstance(model, str)
            or leg.get("harness") not in (None, harness) or not leg.get("family") or not model_family(model)
            or not isinstance(text, str) or not has_files_read(text)
            or not re.search(rf"(?<![0-9a-fA-F]){re.escape(head)}(?![0-9a-fA-F])", text, re.I)
            or (harness == "opencode" and not _tests_reported(text))):
        return None
    return {"model": model, "harness": harness, "witness": "recorded-harness"}


def _quota_refusal(text):
    """Read only provider-shaped quota evidence; keeper never imports runtime."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).replace('\r', '\n')
    marker = r'RESOURCE_EXHAUSTED(?: \(code \d{3}\):|:|$)|HTTP 429\b|(?:individual )?quota (?:reached|exceeded|exhausted|refusal)|usage limit (?:reached|exceeded)|too many requests|rate limit|quota policy filtered \d+ account|All \S+ accounts reached configured quota'
    if re.search(r'(?im)^\s*(?:error:\s*)?(?:' + marker + ')', text):
        return True
    if re.search(r'(?im)^\s*error:.*(?:quota (?:reached|exceeded|exhausted|refusal)|exhausted their quota|rate limit|usage limit|too many requests)', text):
        return True
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            response = event.get('autoos_provider_response') or (event.get('error') if event.get('type') == 'error' else None)
            if isinstance(response, dict) and response.get('status') == 429:
                return True
    return False


def _one_serving_attempt(attempts, model):
    """D-5924: quota-refused legs do not consume another review on the head."""
    if not isinstance(attempts, list) or not attempts:
        return False
    if len(attempts) == 1 and attempts[0] == {}:
        return True  # legacy placeholder with no attempt telemetry
    real = [a for a in attempts if isinstance(a, dict) and a.get('status') != 'skipped']
    if (not real or any(not isinstance(a, dict) for a in attempts)
            or real[-1].get('status') != 'completed'
            or real[-1].get('served_model') != model):
        return False
    for attempt in real[:-1]:
        stdout, stderr = (attempt.get(k) or '' for k in ('stdout_tail', 'stderr_tail'))
        if (attempt.get('status') not in ('failed', 'limited')
                or not all(isinstance(tail, str) for tail in (stderr, stdout))
                or not _quota_refusal(stderr + '\n' + stdout)):
            return False
    return True


class SpawnerRecordAdapter:
    def __init__(self, directory, *, uid=None, local_runs=None, local_host=None):
        self.directory = Path(directory) if directory else None
        self.uid = os.geteuid() if uid is None else uid
        self.local_runs = Path(local_runs).expanduser() if local_runs else None
        self.local_host = local_host

    @classmethod
    def for_keeper(cls, directory, runs_dir):
        """Use the local spawner state when Keeper and the spawner share a host."""
        if not runs_dir:
            state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            state = Path(os.environ.get("PLANGRAPH_SPAWN_STATE") or state_home / "plangraph")
            runs_dir = state / "runs"
        host = (os.environ.get("PLANGRAPH_HOST") or os.environ.get("PLANGRAPH_HOST_NAME")
                or socket.gethostname())
        return cls(directory, local_runs=runs_dir, local_host=host)

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

    def review_seats(self, item, head, decision_text, *, allow_cli_reported=False, allow_empty=False,
                     require_diff_head=False, require_explicit_accept=False, train_delta=None,
                     allow_unwitnessed=False):
        """Read cited review witnesses without inferring a served model."""
        from ..review.family import model_family
        from ..review.verdict import has_files_read
        ids = json.loads(item.get("run_ids") or "[]")
        if not isinstance(ids, list) or any(not isinstance(r, str) for r in ids):
            raise ValueError("invalid item run citations")
        ids = {r for r in ids if not re.fullmatch(r"merged-D-[0-9]+", r)} | set(re.findall(r"(?<![\w-])run-[A-Za-z0-9._-]+", decision_text))
        if not ids or (self.directory is None and self.local_runs is None):
            raise ValueError("no cited records or record mirror")
        seats = []
        for run_id in sorted(ids):
            if not RunRecordAdapter(None).valid_run_id(run_id):
                raise ValueError("invalid cited run id")
            hosts = self._record_hosts(run_id)
            records = [(host, self.record(host, run_id)) for host in hosts]
            records = [(host, record) for host, record in records
                       if record.get("role") == "review"
                       and not record.get("origin_receipt")]
            if hosts and not records:
                continue  # implementation runs and receipts are not witnesses
            if len(records) != 1:
                raise ValueError(f"{run_id}: missing or ambiguous record")
            host, record = records[0]
            if (record.get("run_id") != run_id or record.get("host") != host
                    or record.get("item_key") != item["key"]):
                raise ValueError(f"{run_id}: record identity mismatch")
            if require_diff_head and record.get("head_sha") != head:
                raise ValueError(f"{run_id}: review diff head mismatch")
            model = record.get("served_model")
            family = model_family(model) if isinstance(model, str) else None
            completion = record.get("completion")
            binding = record.get("review_binding")
            completed_binding = completion.get("review_binding") if isinstance(completion, dict) else None
            if ((binding is not None or completed_binding is not None)
                    and (not isinstance(binding, dict) or binding != completed_binding
                         or binding.get("head_sha") != completion.get("head_sha")
                         or binding.get("head_sha") != record.get("head_sha")
                         or not re.fullmatch(r"[0-9a-f]{40}", str(binding.get("base_sha")))
                         or not re.fullmatch(r"[0-9a-f]{64}", str(binding.get("diff_sha256"))))):
                raise ValueError(f"{run_id}: review diff binding mismatch")
            if item.get("review_binding") is not None and item["review_binding"] != binding:
                raise ValueError(f"{run_id}: review diff does not match expected binding")
            attempts = record.get("attempts")
            text = record.get("stdout_tail")
            if isinstance(attempts, list) and len(attempts) > 1:
                final = next((a for a in reversed(attempts) if isinstance(a, dict)
                              and a.get('status') != 'skipped'), {})
                text = final.get('stdout_tail')
            finish = record.get("finish_reason") or (
                completion.get("finish_reason") if isinstance(completion, dict) else None)
            finishes = [part["finish_reason"] for part in (record, completion)
                        if isinstance(part, dict) and "finish_reason" in part]
            pinned = any(isinstance(part, dict) and any(
                k in part and part[k] is not False for k in ("pin", "pinned", "spawn_pin"))
                for part in (record, record.get("plan"), record.get("chosen_leg")))
            cli = record.get("witness_kind") == "cli_reported"
            serving = next((a for a in reversed(attempts or []) if isinstance(a, dict)
                            and a.get('status') != 'skipped'), {}) if isinstance(attempts, list) else {}
            if record.get("review_document") == "train_delta" and train_delta is None:
                raise ValueError(f"{run_id}: train delta seat requires replay evidence")
            if train_delta is not None:
                prompt, excluded = train_delta
                witness = "response-side-gateway-call-log"
                if (record.get("witness_kind") != witness or serving.get("witness_kind") != witness
                        or serving.get("status") != "completed" or serving.get("served_model") != model):
                    raise ValueError(f"{run_id}: train delta needs an authenticated serving attempt")
                if record.get("review_brief_sha256") != prompt:
                    raise ValueError(f"{run_id}: train delta prompt binding mismatch")
                if family in excluded:
                    raise ValueError(f"{run_id}: reviewer family wrote part of the train delta")
                # The router can explicitly skip unused alternates after a successful launch.
                if all(isinstance(a, dict) for a in attempts):
                    attempts = [a for a in attempts if a.get("status") != "skipped"]
                if "finish_reason" in serving:
                    finish = serving["finish_reason"]
                    finishes.append(finish)
                elif finish is None and len(attempts) == 1:
                    # Old dispatch records kept the final step only in the run events.
                    events = record.get("events")
                    if isinstance(events, list):
                        finish = next((e.get("finish_reason") for e in reversed(events)
                                       if isinstance(e, dict) and e.get("event") == "opencode_step_finish"), None)
            if (cli and (not allow_cli_reported or record.get("head_sha") != (completion or {}).get("head_sha")
                         or record.get("verdict") != "pass" or record.get("test_gaming") is not False
                         or not _one_serving_attempt(attempts, model)
                         or serving.get("witness_kind") != "cli_reported"
                         or serving.get("served_model") != model)):
                raise ValueError(f"{run_id}: CLI seat is not permitted or complete")
            recorded = (_recorded_harness_seat(record, completion, text, completion.get("head_sha"))
                        if allow_unwitnessed and model == "unwitnessed" and isinstance(completion, dict) else None)
            if recorded:
                family = model_family(recorded["model"])
            if (not family or (model == "unwitnessed" and not recorded) or pinned or record.get("override_reason")
                    or isinstance(record.get("plan"), dict) and record["plan"].get("p_source") == "override"
                    or record.get("status") != "completed"
                    or type(record.get("exit_code")) is not int or record["exit_code"] != 0
                    or not isinstance(completion, dict)
                    or completion.get("head_sha") not in ([head] if isinstance(head, str) else head)
                    or finish not in ("stop", "end_turn")
                    or any(value not in ("stop", "end_turn") for value in finishes)
                    or not _one_serving_attempt(attempts, model)
                    or not _passing_review(text, require_explicit_accept=require_explicit_accept)
                    or (_required_seat(record) and not has_files_read(text))
                    or (not recorded and record.get("served_family", family) != family)):
                raise ValueError(f"{run_id}: review is not a passing witnessed seat")
            seats.append({"model": recorded["model"] if recorded else model, "verdict": "pass", "run_id": run_id,
                          "head": completion["head_sha"], **({"harness": recorded["harness"],
                                                                "witness": recorded["witness"]} if recorded else {})})
        if not seats and not allow_empty:
            raise ValueError("no cited review witnesses")
        return seats

    def _record_hosts(self, run_id):
        hosts = set()
        if self.directory is not None:
            try:
                hosts.update(p.name for p in self.directory.iterdir() if (p / run_id).exists())
            except FileNotFoundError:
                pass
        if (self.local_host and self.local_runs is not None
                and os.path.lexists(self.local_runs / run_id / "record.json")):
            hosts.add(self.local_host)
        return sorted(hosts)

    def record(self, host, run_id):
        if (not isinstance(host, str) or not RUN_ID_RE.fullmatch(host)
                or host in (".", "..")):
            raise ValueError("invalid spawner host")
        if not RunRecordAdapter(None).valid_run_id(run_id):
            raise ValueError("invalid spawner run id")
        if self.local_runs is not None and host == self.local_host:
            root = self.local_runs.parent.resolve() / self.local_runs.name
            try:
                return self._read_record(root, (run_id,))
            except FileNotFoundError:
                if self.directory is None:
                    raise
        if self.directory is None:
            raise ValueError("spawner_records_dir is not configured")
        root = self.directory.resolve()
        if not (root / host / run_id / "record.json").resolve().is_relative_to(root):
            raise ValueError("record escapes mirror directory")
        return self._read_record(root, (host, run_id))

    def _read_record(self, root, components):
        with ExitStack() as stack:
            def opened(name, flags, parent=None):
                fd = os.open(name, flags, dir_fd=parent)
                stack.callback(os.close, fd)
                return fd

            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
            # Source roots are configured. Apply owner/mode checks to the proof beneath them.
            parent = opened(root, flags)
            for name in components:
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
