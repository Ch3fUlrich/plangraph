"""Pure, fail-closed decision for ONE head; facts are evidence, not authorization.

Reuses seat_verdict's strict parser/family coverage and ci_commit_gate's door
table. No Git, clock, environment, keeper, network or file access in decide().
"""
from dataclasses import dataclass
import fnmatch
import re

import ci_commit_gate
import seat_verdict


@dataclass(frozen=True)
class Decision:
    outcome: str
    code: str | None = None
    detail: str = ""
    sha: str = ""


def _refuse(code, detail):
    return Decision("REFUSE", code, detail)


def _strings(value):
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _path(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    parts = value.replace("\\", "/").lower().split("/")
    if any(p in ("", "..") or ":" in p for p in parts):
        return None
    return "/".join(p for p in parts if p != ".") or None


def _gate_path(path):
    # Import the existing authority, never a snapshot of its path list.
    if any(fnmatch.fnmatchcase(path, p) for p in
           (*ci_commit_gate.AUTH_DOOR_PATHS, *ci_commit_gate.GATE_PATHS)):
        return True
    # Additional categories in D-5252 not yet covered by the trailer door table.
    name = path.rsplit("/", 1)[-1]
    return (path == ".docs-map" or path.startswith(("deploy/", ".filesize-", ".gitleaks"))
            or any(word in path for word in ("allow", "redact", "auth"))
            or (path.startswith("tools/") and
                (any(word in name for word in ("gate", "check", "preflight", "ci_", "red_proof"))
                 or "/split/" in path)))


def decide(facts):
    """Return one Decision; first refusal wins in the documented order.

    ci_checks is the complete latest-per-name set, each with name/head_sha/
    status/conclusion. main_changed_production_paths is main's complete delta
    since CI's base (rule 3.19), including old rename/deletion paths. The caller
    must similarly include both sides of PR renames in changed_paths.
    """
    if not isinstance(facts, dict):
        return _refuse("ci_pending", "facts unavailable or invalid")
    head = facts.get("head_sha_now")
    if (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
            or facts.get("head_sha_at_check") != head):
        return _refuse("head_moved", "head changed or unverified")
    checks, required = facts.get("ci_checks"), facts.get("required_checks")
    if not isinstance(checks, list) or not checks or not all(isinstance(c, dict) for c in checks):
        return _refuse("ci_pending", "checks absent or invalid")
    current = [c for c in checks if c.get("head_sha") == head]
    green = ("success", "skipped")  # skipped suites also pass merge_batch's GREEN policy
    if any(c.get("status") == "completed" and c.get("conclusion") not in (*green, None, "")
           for c in current):
        return _refuse("ci_red", "completed check is not green")
    names = [c.get("name") for c in checks]
    if (not _strings(required) or not required or not _strings(names)
            or len(set(names)) != len(names) or not set(required).issubset(names)
            or len(current) != len(checks)
            or any(c.get("name") in required and c.get("conclusion") != "success" for c in current)
            or any(c.get("status") != "completed" or c.get("conclusion") not in green for c in current)):
        return _refuse("ci_pending", "checks incomplete or not bound to head")
    seats = facts.get("seats")
    if not isinstance(seats, list) or not seats or not all(isinstance(s, dict) for s in seats):
        return _refuse("seat_missing", "review seat absent or invalid")
    if any(s.get("pinned") is not False for s in seats):
        return _refuse("seat_pinned", "pinned or unverified seat")
    writers = facts.get("writer_families")
    families = [s.get("family") for s in seats]
    if (not _strings(writers) or not writers or not _strings(families)
            or any(f not in seat_verdict.KNOWN_FAMILIES for f in writers + families)
            or set(writers).intersection(families)):
        return _refuse("seat_family_is_writer", "writer/seat families overlap or are unknown")
    if any(not isinstance(s.get("open_findings"), list)
           or any(level != "LOW" for level in s["open_findings"]) for s in seats):
        return _refuse("finding_open", "open blocking or unknown finding")
    accepted = [s["family"] for s in seats if isinstance(s.get("verdict"), str)
                and seat_verdict.parse_verdict(s["verdict"])[0] is True]
    if not accepted or seat_verdict.coverage(writers, accepted, need=1):
        return _refuse("seat_missing", "no independent accepting seat")
    paths = facts.get("changed_paths")
    if (not _strings(paths) or not paths
            or any(_path(p) is None or _gate_path(_path(p)) for p in paths)):
        return _refuse("gate_path", "protected or unverified changed path")
    if facts.get("tier") != "standard":
        return _refuse("tier_high", "tier is not standard")
    key = facts.get("item_key")
    if not isinstance(key, str) or not key.strip() or facts.get("plan_key") != key:
        return _refuse("plan_key_mismatch", "PLAN key absent or differs from item")
    if facts.get("preflight_present") is not True:
        return _refuse("preflight_missing", "preflight record absent")
    main_paths = facts.get("main_changed_production_paths")
    if (not _strings(main_paths) or any(_path(p) is None for p in main_paths)
            or {_path(p) for p in paths}.intersection(_path(p) for p in main_paths)):
        return _refuse("stale_green", "main production delta overlaps PR or is unknown")
    return Decision("GO", sha=head)
