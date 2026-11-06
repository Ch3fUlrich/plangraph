"""Private review material snapshots, strict replies, and finding dispositions."""
from __future__ import annotations

import difflib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from plangraph.review.verdict import judge


def validate_directory(directory):
    directory = Path(directory).resolve()
    root = Path(subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], cwd=Path(__file__).parent,
        text=True).strip()).resolve()
    if directory.is_relative_to(root):
        raise ValueError("material directory must be outside the checkout")
    return directory


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def material(directory, max_file_chars=200000):
    directory = validate_directory(directory)
    paths = [directory / "plan.md"] + sorted((directory / "evidence").rglob("*"))
    files = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError("material symlinks are refused")
        if path.is_dir():
            continue
        data = path.read_bytes()
        if ".export." in path.name or len(data.decode("utf-8")) > max_file_chars:
            raise ValueError("unsafe material file")
        files[path.relative_to(directory).as_posix()] = data
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    digest = hashlib.sha256("".join(f"{hashes[p]}  {p}\n" for p in sorted(hashes)).encode()).hexdigest()
    return digest, files, hashes


def require_frozen(directory):
    directory = validate_directory(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if material(directory)[0] != manifest["text_sha"]:
        raise ValueError("material changed; freeze before reviewing")
    return manifest


def freeze(directory, *, minor=False, fixes=(), rows=None, minor_max_chars=2000,
           max_file_chars=200000, growth_cite=None):
    directory = validate_directory(directory)
    sha, files, hashes = material(directory, max_file_chars)
    path = directory / "manifest.json"
    old = json.loads(path.read_text()) if path.exists() else {"history": []}
    selected = []
    if minor:
        if not old.get("text_sha") or not fixes:
            raise ValueError("minor freeze needs a previous snapshot and findings")
        by_id = {row["id"]: row for row in rows or []}
        selected = [by_id.get(key) for key in fixes]
        if any(not row or row["severity"] != "minor" or row["disposition"] != "open" for row in selected):
            raise ValueError("minor freeze fixes must be open minors")
        previous = directory / "frozen" / old["text_sha"]
        before = {p.relative_to(previous).as_posix(): p.read_bytes() for p in previous.rglob("*") if p.is_file()}
        changed = 0
        for name in before.keys() | files.keys():
            diff = difflib.ndiff(before.get(name, b"").decode().splitlines(True), files.get(name, b"").decode().splitlines(True))
            changed += sum(len(line[2:]) for line in diff if line.startswith(("+ ", "- ")))
        if changed > minor_max_chars:
            raise ValueError("minor diff exceeds limit")
    snapshot = directory / "frozen" / sha
    for name, data in files.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("snapshot integrity failure")
        target.write_bytes(data)
    result = dict(old, text_sha=sha, files=hashes)
    if sha != old.get("text_sha"):
        result["history"] = old["history"] + [{"sha": sha, "material": not minor,
                                                "from_sha": old.get("text_sha"), "growth_cite": growth_cite}]
    # Keep finding updates in the same atomic manifest transaction. The driver
    # can recover the ledger projection after interruption before its own save.
    if minor:
        result["minor_fixes"] = list(fixes)
        projection = copy.deepcopy(rows)
        for row in projection:
            if row["id"] in fixes:
                row.update(disposition="accepted", sha_fixed=sha, closed_by="")
        result["ledger_projection"] = projection
    atomic_json(path, result)
    if minor:
        atomic_json(directory / "ledger.json", projection)
    for row in selected:
        row.update(disposition="accepted", sha_fixed=sha, closed_by="")
    return result


def start_discovery(directory):
    manifest = require_frozen(directory)
    if "plan_chars_r1" not in manifest:
        manifest["plan_chars_r1"] = len((Path(directory) / "plan.md").read_text(encoding="utf-8"))
        atomic_json(Path(directory) / "manifest.json", manifest)
    return manifest


def parse_reply(text):
    def failed(reason):
        return {"status": "failed", "reason": reason, "verdict": None, "findings": []}
    if "[... output truncated ...]" in text:
        return failed("output_truncated")
    blocks = re.findall(r"(?m)^```plan-review[ \t]*\n(.*?)^```[ \t]*$", text, re.S)
    if len(blocks) != 1 or len(re.findall(r"(?m)^```plan-review[ \t]*$", text)) != 1:
        return failed("invalid_block")
    findings = []
    for line in blocks[0].splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split("|")]
        scope = len(parts) == 8 and parts[-1] == "SCOPE+"
        if scope:
            parts.pop()
        if (len(parts) != 7 or not all(parts) or not re.fullmatch(r"F\d+-\d+", parts[0])
                or parts[1] not in {"blocker", "major", "minor"} or not re.fullmatch(r"[\w-]+", parts[2])):
            return failed("invalid_finding")
        finding = dict(zip(("seat_finding_id", "severity", "class", "location", "claim", "evidence", "fix"), parts))
        finding.update(scope_plus=scope, unverified=parts[5] == "UNVERIFIED")
        findings.append(finding)
    verdict = judge(text, None).verdict
    if verdict is None or not text.rstrip().splitlines()[-1].startswith("VERDICT:"):
        return failed("no_verdict")
    changes = verdict != "pass" or any(f["severity"] in {"blocker", "major"} for f in findings)
    return {"status": "completed", "reason": "", "verdict": "REQUEST_CHANGES" if changes else "ACCEPT", "findings": findings}


def parse_record(record):
    attempts = record.get("attempts") or []
    return parse_reply((attempts[-1].get("stdout_tail") or "") if attempts else "")


def ingest(rows, parsed, *, round, seat_id, family, lens, sha):
    if parsed["status"] != "completed":
        return []
    known = {row["class"] for row in rows}
    new = []
    for finding in parsed["findings"]:
        suffix = finding["seat_finding_id"].split("-")[-1]
        key = f"R{round}.{seat_id}.{suffix}"
        if any(row["id"] == key for row in rows):
            raise ValueError("duplicate finding id")
        row = dict(finding, id=key, seat_id=seat_id, round=round, family=family, lens=lens,
                   disposition="open", disposition_evidence="", trigger="", sha_seen=sha,
                   sha_fixed="", closed_by="", sightings=1, dup_of="")
        if row["class"] not in known:
            new.append(row["class"])
            known.add(row["class"])
        rows.append(row)
    return new


def for_seats(rows):
    result = []
    for row in rows:
        copy = {k: v for k, v in row.items() if k not in {"family", "lens", "seat_id"}}
        copy["id"] = re.sub(r"^(R\d+)\.[^.]+\.", r"\1.", row["id"])
        if copy.get("dup_of"):
            copy["dup_of"] = re.sub(r"^(R\d+)\.[^.]+\.", r"\1.", copy["dup_of"])
        result.append(copy)
    return result


def dispose(rows, finding_id, disposition, manifest, *, sha_fixed="", disposition_evidence="",
            trigger="", dup_of="", third_party="", planner_family="", seats=(),
            tier="standard", fact_check="", claims=None):
    row = next(row for row in rows if row["id"] == finding_id)
    history = [entry["sha"] for entry in manifest["history"]]
    if tier == "high" and row["unverified"] and row["severity"] in {"blocker", "major"}:
        claim = (claims or {}).get(fact_check, {})
        if claim.get("sha") != manifest["text_sha"] or claim.get("state") not in {"verified", "false"}:
            raise ValueError("current fact check required")
    if dup_of:
        target = next(r for r in rows if r["id"] == dup_of)
        if rows.index(target) >= rows.index(row) or target.get("dup_of"):
            raise ValueError("duplicate must name an earlier primary finding")
        target["sightings"] += row["sightings"]
        row["dup_of"] = dup_of
        return row
    if disposition == "accepted":
        if sha_fixed not in history or row["sha_seen"] not in history or history.index(sha_fixed) <= history.index(row["sha_seen"]):
            raise ValueError("accepted fix needs a newer frozen sha")
    elif disposition == "rejected":
        if not disposition_evidence:
            raise ValueError("rejection needs evidence")
        if row["severity"] == "blocker" and not row["unverified"]:
            if not planner_family or not any(s.get("run_id") == third_party and s.get("status") == "completed"
                                            and s.get("family") != planner_family for s in seats):
                raise ValueError("verified blocker needs independent review")
    elif disposition == "deferred":
        if not trigger:
            raise ValueError("deferral needs a trigger")
    else:
        raise ValueError("unknown disposition")
    row.update(disposition=disposition, sha_fixed=sha_fixed, disposition_evidence=disposition_evidence, trigger=trigger)
    return row


def close_accepted(rows, manifest, seats):
    history = [entry["sha"] for entry in manifest["history"]]
    for row in rows:
        if row["disposition"] != "accepted" or row["closed_by"]:
            continue
        for seat in seats:
            if (seat.get("status") == "completed" and seat.get("witnessed") and seat.get("sha") in history
                    and history.index(seat["sha"]) >= history.index(row["sha_fixed"])):
                row["closed_by"] = seat["run_id"]
                break
