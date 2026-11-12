"""Pure, fail-closed decision for ONE head; facts are evidence, not authorization.

Reuses seat_verdict's strict parser/family coverage and ci_commit_gate's door
table. No Git, clock, environment, keeper, network or file access in decide().
"""
from dataclasses import dataclass
import fnmatch
import re

import ci_commit_gate
import seat_verdict

FACT_KEYS = frozenset({
    "head_sha_at_check", "head_sha_now", "required_checks", "ci_checks", "seats",
    "writer_families", "changed_paths", "tier", "plan_key", "item_key",
    "preflight_present", "main_changed_production_paths"})
CHECK_KEYS = frozenset({"name", "head_sha", "status", "conclusion"})
SEAT_KEYS = frozenset({"family", "verdict", "pinned", "open_findings"})


@dataclass(frozen=True)
class Decision:
    outcome: str
    code: str | None = None
    detail: str = ""
    sha: str = ""


def _refuse(code, detail):
    return Decision("REFUSE", code, detail)


def _text(value):
    return (isinstance(value, str) and bool(value) and value.strip() == value
            and value.isprintable() and value.upper() != "UNKNOWN")


def _strings(value):
    return isinstance(value, list) and all(_text(v) for v in value)


def _keys(value, expected):
    return (isinstance(value, dict) and all(isinstance(k, str) for k in value)
            and value.keys() == expected)


def _path(value):
    if not _text(value):
        return None
    parts = value.replace("\\", "/").lower().split("/")
    if any(p in ("", "..", "unknown") or ":" in p for p in parts):
        return None
    return "/".join(p for p in parts if p != ".") or None


def _door_table_valid(table):
    if not isinstance(table, (tuple, list)) or not table:
        return False
    normalized = [_path(p) for p in table]
    return (all(normalized) and len(set(normalized)) == len(normalized)
            and all("/" in p or p in ("roles*", "scopes*") for p in table))


def _gate_path(path):
    # Import the existing authority, never a snapshot of its path list.
    tables = (getattr(ci_commit_gate, "AUTH_DOOR_PATHS", None),
              getattr(ci_commit_gate, "GATE_PATHS", None))
    if not all(_door_table_valid(table) for table in tables):
        return True  # An invalid authority cannot establish any path as safe.
    if any(fnmatch.fnmatchcase(path, _path(p)) for table in tables for p in table):
        return True
    # Additional categories in D-5252 not yet covered by the trailer door table.
    name = path.rsplit("/", 1)[-1]
    return (path == ".docs-map" or path.startswith(("deploy/", ".filesize-", ".gitleaks"))
            or any(word in path for word in ("allow", "redact", "auth"))
            or (path.startswith("tools/") and
                (any(word in name for word in ("gate", "check", "scrub", "preflight", "ci_", "red_proof"))
                 or "/split/" in path)))


def decide(facts):
    """Return one Decision; first refusal wins in the documented order.

    ci_checks is the complete latest-per-name set, each with name/head_sha/
    status/conclusion. main_changed_production_paths is main's complete delta
    since CI's base (rule 3.19), including old rename/deletion paths. The caller
    must similarly include both sides of PR renames in changed_paths.
    """
    if not _keys(facts, FACT_KEYS):
        return _refuse("ci_pending", "facts unavailable or invalid")
    head = facts.get("head_sha_now")
    if (not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
            or facts.get("head_sha_at_check") != head):
        return _refuse("head_moved", "head changed or unverified")
    checks, required = facts.get("ci_checks"), facts.get("required_checks")
    if (not isinstance(checks, list) or not checks
            or not all(_keys(c, CHECK_KEYS) for c in checks)
            or any(not isinstance(c[k], str) for c in checks for k in CHECK_KEYS)):
        return _refuse("ci_pending", "checks absent or invalid")
    current = [c for c in checks if c.get("head_sha") == head]
    green = ("success", "skipped")  # skipped suites also pass merge_batch's GREEN policy
    if any(c.get("status") == "completed" and c.get("conclusion") not in (*green, None, "")
           for c in current):
        return _refuse("ci_red", "completed check is not green")
    names = [c.get("name") for c in checks]
    if (not _strings(required) or not required or not _strings(names)
            or len(set(required)) != len(required) or len(set(names)) != len(names)
            or not set(required).issubset(names)
            or len(current) != len(checks)
            or any(c.get("name") in required and c.get("conclusion") != "success" for c in current)
            or any(c.get("status") != "completed" or c.get("conclusion") not in green for c in current)):
        return _refuse("ci_pending", "checks incomplete or not bound to head")
    seats = facts.get("seats")
    if (not isinstance(seats, list) or not seats
            or not all(_keys(s, SEAT_KEYS) for s in seats)):
        return _refuse("seat_missing", "review seat absent or invalid")
    if any(s.get("pinned") is not False for s in seats):
        return _refuse("seat_pinned", "pinned or unverified seat")
    writers = facts.get("writer_families")
    families = [s.get("family") for s in seats]
    if (not _strings(writers) or not writers or not _strings(families)
            or len(set(writers)) != len(writers)
            or any(f not in seat_verdict.KNOWN_FAMILIES for f in writers + families)
            or set(writers).intersection(families)):
        return _refuse("seat_family_is_writer", "writer/seat families overlap or are unknown")
    if any(not isinstance(s.get("open_findings"), list)
           or any(level != "LOW" for level in s["open_findings"])
           or len(s["open_findings"]) > 1 for s in seats):
        return _refuse("finding_open", "open blocking or unknown finding")
    accepted = [s["family"] for s in seats if isinstance(s.get("verdict"), str)
                and seat_verdict.parse_verdict(s["verdict"])[0] is True]
    if (len(accepted) != len(seats) or len(set(families)) != len(families)
            or seat_verdict.coverage(writers, accepted, need=1)):
        return _refuse("seat_missing", "no independent accepting seat")
    paths = facts.get("changed_paths")
    if (not _strings(paths) or not paths
            or any(_path(p) is None or _gate_path(_path(p)) for p in paths)
            or len({_path(p) for p in paths}) != len(paths)):
        return _refuse("gate_path", "protected or unverified changed path")
    if facts.get("tier") != "standard":
        return _refuse("tier_high", "tier is not standard")
    key = facts.get("item_key")
    if (not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", key)
            or any(part.upper() == "UNKNOWN" for part in key.split(":"))
            or facts.get("plan_key") != key):
        return _refuse("plan_key_mismatch", "PLAN key absent or differs from item")
    if facts.get("preflight_present") is not True:
        return _refuse("preflight_missing", "preflight record absent")
    main_paths = facts.get("main_changed_production_paths")
    if (not _strings(main_paths) or any(_path(p) is None for p in main_paths)
            or len({_path(p) for p in main_paths}) != len(main_paths)
            or {_path(p) for p in paths}.intersection(_path(p) for p in main_paths)):
        return _refuse("stale_green", "main production delta overlaps PR or is unknown")
    return Decision("GO", sha=head)
