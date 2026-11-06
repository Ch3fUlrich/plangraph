"""Advisory batch checks and read-only create coverage reporting.

Both public verbs return (exit_code, output_lines); CLI wiring lives elsewhere.
The ops digest hashes the exact batch bytes, as apply_batch does, not a YAML
reserialization. Checking never applies operations or records a decision.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tomllib

# The batch tools use script-style sibling imports.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import apply_batch
from tools import plan_loop_ledger as ledger
from tools import plan_loop_stop as stop


COVERAGE = "coverage: creates only; item_defined not served by any read verb"


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _records(path):
    records = _json(path) if path.exists() else []
    if not isinstance(records, list) or any(
        not isinstance(row, dict)
        or not isinstance(row.get("ops_sha"), str)
        or not isinstance(row.get("op_keys"), list)
        or not all(isinstance(key, str) for key in row["op_keys"])
        for row in records
    ):
        raise ValueError("invalid batch records")
    return records


def _state(directory):
    manifest = ledger.require_frozen(directory)
    policy = tomllib.loads((directory / "plan-loop.toml").read_text(encoding="utf-8"))
    rows = _json(directory / "ledger.json")
    seats = [json.loads(line) for line in
             (directory / "yield.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    # Stop evaluation expects the driver-normalized report and claims. Support
    # on-disk claim dispositions too, without reinterpreting report text here.
    claims = directory / "r0" / "claims.json"
    if claims.exists():
        manifest["claims"] = _json(claims)
    manifest["plan_chars"] = len((directory / "plan.md").read_text(encoding="utf-8"))
    return stop.evaluate(rows, manifest, seats, policy), manifest


def _op_keys(ops):
    keys = []
    for op in ops:
        apply_batch.validate_op(op)
        if op["op"] == "create":
            items = op["item"] if isinstance(op["item"], list) else [op["item"]]
            keys.extend(f"{item['project']}:{item['id']}" for item in items)
        elif op.get("key"):
            keys.append(op["key"])
    return list(dict.fromkeys(keys))


def check_batch(directory, batch, *, waived_ok=False):
    """Check current review status, source digest and ops, then record coverage.

    batches.json is a list of {ops_sha, op_keys}; repeated checks are idempotent.
    A waiver is never inferred from the flag: evaluation must actually be WAIVED.
    Refusals do not write a record and do not expose private input or paths.
    """
    try:
        directory = ledger.validate_directory(directory)
        status, manifest = _state(directory)
        if status.state != "CLEAN" and not (status.state == "WAIVED" and waived_ok):
            return 1, [status.line, "refused: batch requires CLEAN or WAIVED with --waived-ok"]
        batch = Path(batch)
        original = batch.read_bytes()
        data = apply_batch.load_batch(batch)
        if data["source_sha256"] != manifest["text_sha"]:
            return 1, ["refused: source_sha256 does not match frozen material"]
        keys = _op_keys(data["ops"])
        # Refuse a concurrent edit rather than associating parsed ops with
        # different bytes. The next invocation must check the edited batch.
        if batch.read_bytes() != original:
            return 1, ["refused: batch changed during check"]
        digest = hashlib.sha256(original).hexdigest()
        path = directory / "batches.json"
        records = _records(path)
        record = {"ops_sha": digest, "op_keys": keys}
        if record not in records:
            ledger.atomic_json(path, records + [record])
        lines = [f"ops_sha={digest}",
                 "reminder: someone other than the plan's author records this digest as a D-row"]
        if status.state == "WAIVED":
            lines.append(f"{status.line} allowed by --waived-ok")
        return 0, lines
    except (OSError, ValueError, TypeError, KeyError, AttributeError, apply_batch.BatchError):
        return 1, ["refused: unreadable or invalid check input"]


def report(root, changelog):
    """Match keeper changelog creates against all checked batch keys under root.

    No subprocesses, network calls, or writes. Invalid input is not silently
    treated as an empty changelog or empty coverage. Coverage is always stated.
    """
    try:
        root = Path(root)
        if not root.is_dir():
            raise ValueError("invalid report root")
        data = _json(Path(changelog))
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError("invalid changelog entries")
        matched = set()
        for path in sorted(root.rglob("batches.json")):
            for row in _records(path):
                matched.update(row["op_keys"])
        lines = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
                raise ValueError("invalid changelog entry")
            if entry["type"] != "created":
                continue
            key, actor, ts = (entry[field] for field in ("key", "by", "ts"))
            if not all(isinstance(value, str) for value in (key, actor, ts)):
                raise ValueError("invalid create entry")
            if key not in matched:
                # Keep the one-line contract even for untrusted event fields.
                key, actor, ts = (" ".join(value.split()) for value in (key, actor, ts))
                lines.append(f"UNMATCHED {key} by={actor} ts={ts}")
        return 0, lines + [COVERAGE]
    except (OSError, ValueError, TypeError, KeyError):
        return 1, ["error: unreadable or invalid report input", COVERAGE]
