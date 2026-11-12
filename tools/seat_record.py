#!/usr/bin/env python3
"""Derive a review seat's evidence + review text from a spawner record.json.

A spawner seat run dir (``~/.local/state/plangraph/runs/<run>/``) holds only
``record.json`` and ``live.log``.  ``evidence_from_record`` turns that into the
``evidence.json``/``review.txt`` pair tools/seat_verdict.py consumes, failing
closed (ValueError) whenever the record cannot witness a completed review.
The record is bound to its run dir (run_id must equal the dir name) and must be
uid-owned, not group/world-writable.  ``--head`` binds the completion-time HEAD
of the run cwd (record completion.head_sha), not the launch commit: the spawner
records no launch-time sha, so a launch binding cannot be checked here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
_HEAD_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
# F8: both spellings count (review/tail.md uses TEST-GAMING).
_TEST_GAMING = re.compile(r"^TEST[_-]GAMING:[ \t]*(.*)$", re.I | re.M)

def _canon_family(name):
    """Canonical family of a model or family name via seat_verdict's alias table."""
    from seat_verdict import model_family, KNOWN_FAMILIES  # local: seat_verdict imports this module
    family = model_family(name) or str(name or "").strip().lower()
    if family not in KNOWN_FAMILIES:
        raise ValueError(f"unknown model family {name!r}")
    return family

def evidence_from_record(run_dir: Path, cli_writer_families=None, *, record=None) -> tuple[dict, str]:
    """Return (evidence, review_text) for a record.json-only seat run dir.

    Writer coverage comes only from the CLI --writer-family list (enforced in
    seat_verdict.main); ``cli_writer_families`` is accepted for call compat but
    ignored here, because record.writer_family is the seat's own leg family and
    never names a reviewed writer.  Raises ValueError whenever the record
    cannot witness a completed review.
    """
    run_dir = Path(run_dir)
    record_path = run_dir / "record.json"
    try:
        if record is None:
            record = json.loads(record_path.read_text("utf-8"))
        stat = record_path.stat()
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"record.json is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError("record.json is not an object")
    # F6: bind the record to its directory and to this uid's write access.
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise ValueError("record.json must be owned by the current uid and "
                         "not group/world-writable")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        raise ValueError(f"record run_id {run_id!r} does not match "
                         f"run dir name {run_dir.name!r}")
    if record.get("role") != "review":
        raise ValueError(f"record role {record.get('role')!r} is not 'review'")
    if (any(isinstance(part, dict) and any(k in part and part[k] is not False
            for k in ("pin", "pinned", "spawn_pin"))
            for part in (record, record.get("plan"), record.get("chosen_leg")))
            or record.get("override_reason") or isinstance(record.get("plan"), dict)
            and record["plan"].get("p_source") == "override"):
        raise ValueError("pinned seat")
    if record.get("status") != "completed":
        raise ValueError(f"record status {record.get('status')!r} is not 'completed'")
    exit_code = record.get("exit_code")
    if type(exit_code) is not int or exit_code != 0:
        raise ValueError(f"record exit_code {exit_code!r} is not 0")
    completion = record.get("completion")
    head_sha = completion.get("head_sha") if isinstance(completion, dict) else None
    if not isinstance(head_sha, str) or not _HEAD_SHA.match(head_sha):
        raise ValueError("record completion.head_sha missing or not 40 hex chars")
    try:
        live_log = (run_dir / "live.log").read_text("utf-8")
    except OSError:
        live_log = ""
    if live_log.strip():
        review_text = live_log
    else:
        # F4: stdout_tail accumulates every leg (service.py/runner.py append to
        # it), so it may stand in for live.log only on a single-attempt record.
        attempts = record.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > 1:
            raise ValueError("stdout_tail accumulates every leg; live.log required")
        review_text = str(record.get("stdout_tail") or "")
    if not review_text.strip():
        raise ValueError("record has no review text (live.log and stdout_tail empty)")
    served = record.get("served_model")
    leg = record.get("chosen_leg")
    chosen = leg.get("model") if isinstance(leg, dict) else None
    leg_family = leg.get("family") if isinstance(leg, dict) else None
    if leg_family is not None and _canon_family(leg_family) != _canon_family(chosen):
        raise ValueError(f"chosen_leg.family {leg_family!r} does not match "
                         f"family {_canon_family(chosen)!r} of chosen_leg.model {chosen!r}")
    evidence = {"head_sha": head_sha, "source": "record.json", "run_id": run_id}
    # F3: finish_reason is only ever recorded; never hard-coded.  With no
    # record finish field the key is omitted, so a PASS is refused downstream.
    finish = record.get("finish_reason")
    if not (isinstance(finish, str) and finish) and isinstance(completion, dict):
        finish = completion.get("finish_reason")
    if isinstance(finish, str) and finish:
        evidence["finish_reason"] = finish
    if isinstance(served, str) and served and served != "unwitnessed":
        evidence["served_model"] = served
    else:
        if not isinstance(chosen, str) or not chosen:
            raise ValueError("record chosen_leg.model missing; seat model unwitnessed")
        # F2/D-2905: an unwitnessed served model may be replaced by the chosen
        # leg only on an opencode/*-free leg (the request-side witness rule);
        # any other leg never witnesses a family.
        if not (chosen.startswith("opencode/") and chosen.endswith("-free")):
            leg_id = leg.get("id") if isinstance(leg, dict) else None
            raise ValueError(f"served_model unwitnessed and chosen leg {leg_id!r} "
                             f"({chosen!r}) is not an opencode/*-free leg (D-2905); "
                             f"family not witnessed")
        evidence.update(served_model=chosen, witness_kind="record-chosen-leg",
                        request_side_model=chosen)
    from seat_verdict import _VERDICT_LINE as verdict_line, parse_verdict
    lines = review_text.splitlines()
    verdicts = {" ".join(m.group(1).split()).lower()
                for line in lines
                for m in [verdict_line.match(line)] if m}
    if len(verdicts) > 1:
        raise ValueError("conflicting VERDICT lines")
    last = parse_verdict(review_text)[1]
    # F8 (D-3420): "VERDICT must be the last line" stays strict until the
    # routing rules say otherwise; only TEST[_-]GAMING lines may follow it.
    if last >= 0 and any(line.strip() and not _TEST_GAMING.match(line)
                         for line in lines[last + 1:]):
        raise ValueError("verdict is not the last line")
    gaming = None
    for match in _TEST_GAMING.finditer(review_text):
        gaming = match.group(1).strip()
    if gaming and "|" not in gaming:
        evidence["test_gaming"] = gaming.split()[0].lower()
    return evidence, review_text

class RecordIndex(dict):
    """Records plus their lexical root, excluding unrelated system ancestors."""
def _has_symlink(path, index):
    root = getattr(index, "root", path.parent.parent).absolute()
    relative = path.absolute().relative_to(root)
    return any((root / p).is_symlink() for p in (relative, *relative.parents))
def record_index(records_dir):
    """Read each record and prompt once, shared by writer and review discovery."""
    if isinstance(records_dir, dict):
        return records_dir
    rows = RecordIndex()
    rows.root = Path(records_dir or Path.home() / ".local/share/plangraph/spawner-records").absolute()
    rows.skips = []
    for directory, dirs, files in os.walk(rows.root, followlinks=False):
        parent = Path(directory)
        links = {name for name in dirs if (parent / name).is_symlink()}
        rows.skips.extend(f"symlinked directory skipped: {(parent / name).relative_to(rows.root)}"
                          for name in sorted(links))
        dirs[:] = sorted(set(dirs) - links)
        if not parent.name.startswith("run-") or "record.json" not in files:
            continue
        path = parent / "record.json"
        try:
            record = json.loads(path.read_text("utf-8"))
            if not isinstance(record, dict):
                continue
            try:
                prompt = (path.parent / "prompt.txt").read_text("utf-8")
            except OSError:
                prompt = ""
            rows[path] = (record, prompt)
        except (OSError, ValueError):
            continue
    return rows

def merged_pr(item, git, lookup=None):
    """Resolve only an evidenced commit; injected lookup may use a JSON cache."""
    from plangraph.keeper.evidence_repo import repo_key
    head = item.get("head_sha")
    if not head: return None
    if lookup is not None:
        try:
            value = lookup(head)
            return int(value) if (type(value) is int or isinstance(value, str)
                                 and re.fullmatch(r"[0-9]+", value)) and int(value) > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    repo = git.repo_path(repo_key(item, SimpleNamespace(git=git), head)) if git else None
    if repo:
        try:
            result = subprocess.run(["git", "show", "-s", "--format=%s", head],
                                    cwd=repo, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"Merge PR #(\d+)\b", result.stdout)
        return int(match[1]) if result.returncode == 0 and match else None
    return None

def writer_families(item, head, records_dir, git):
    """Union evidenced git writers and matching implement-record writers."""
    from plangraph.keeper.evidence_repo import repo_key, track_key
    from seat_verdict import model_family
    models = []
    if git is not None and head:
        key = repo_key(item, SimpleNamespace(git=git), head)
        repo, configured = git.repo_path(key), getattr(git, "_repos", None)
        if not repo and (not isinstance(configured, dict)
                         or key in configured or track_key(item) in configured): return []
        if repo:
            try:
                rows = git.writer_trailers(repo, head, git.repo_ref(key))
            except (OSError, subprocess.SubprocessError): return []
            for trailers in rows or [{}]:
                writers = [v.split()[0] for name, value in trailers.items() if name.lower() == "writer"
                           for v in (value if isinstance(value, list) else [value]) if v.split()]
                if not writers: return []
                models.extend(writers)
    if records_dir is not None:
        index = record_index(records_dir)
        for path, (r, _) in index.items():
            try:
                if (r.get("role") == "implement" and r.get("item_key") == item["key"]
                        and not _has_symlink(path, index)):
                    models.append(r.get("served_model"))
            except (OSError, ValueError, AttributeError):
                continue
    families = {model_family(m) for m in models}
    return sorted(families) if None not in families else []

def snapshot_item(item, records_dir=None, git=None):
    """Choose newest verified evidence; conflicting SHAs in its epoch refuse."""
    if not isinstance(item.get("key"), str) or not item["key"]:
        raise ValueError("snapshot item missing key")
    rows = []
    evidence = item.get("evidence")
    for e in evidence if isinstance(evidence, list) else []:
        if (not isinstance(e, dict) or e.get("kind") not in ("commit", "commit+ci")
                or e.get("verified") is not True):
            continue
        ref = e.get("ref")
        if isinstance(ref, dict):
            ref = ref.get("sha") or ref.get("commit")
        if isinstance(ref, str) and _HEAD_SHA.fullmatch(ref):
            rows.append((e, ref.lower()))
    head, reason = None, "no commit evidence"
    if rows:
        def order(row):
            from datetime import datetime
            at = row[0].get("at")
            try:
                stamp = float(at)
            except (TypeError, ValueError):
                try:
                    stamp = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    stamp = float("-inf")
            epoch = row[0].get("epoch")
            return stamp, epoch if type(epoch) is int else -1
        newest, sha = max(rows, key=order)
        heads = {s for e, s in rows if e.get("epoch") == newest.get("epoch")}
        head = sha if len(heads) == 1 else None
        reason = None if head else "head mismatch: ambiguous verified commits at same epoch"
    return dict(item, head_sha=head, missing_reason=reason,
                writer_families=writer_families(item, head, records_dir, git))

def eligible_records(item, records_dir, known_keys=()):
    """Pure local discovery: return (eligible records, rejection reasons).
    Schema/ownership checks cannot exclude fabrication by a malicious same-uid writer.
    No network, writes, tokens, or keeper mutations occur here.
    """
    from seat_verdict import KNOWN_FAMILIES, model_family, parse_verdict
    head, writers = item.get("head_sha"), item.get("writer_families")
    if not isinstance(head, str) or not _HEAD_SHA.fullmatch(head):
        return [], [item.get("missing_reason") or "head mismatch: missing or ambiguous evidenced commit"]
    if not writers or any(w not in KNOWN_FAMILIES for w in writers):
        return [], ["unknown writer"]
    eligible, reasons, bound = [], [], False
    index, known_keys = record_index(records_dir), set(known_keys) | {item["key"]}
    for path, (record, prompt) in index.items():
        family = model_family(record.get("served_model"))
        try:
            if record.get("role") != "review": continue
            prs = {int(n) for n in re.findall(r"\bPR\s+#(\d+)\b", prompt, re.I)}
            keys = {k.rstrip(".") for k in re.findall(r"(?<![\w.:-])[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?![\w.:-])", prompt)} & known_keys
            named = bool(re.search(r"(?<![\w:])" + re.escape(item["key"]) + r"(?![\w-])", prompt))
            if record.get("item_key") != item["key"] and not (named or item.get("pr_number") in prs): continue
            bound = True
            if record.get("item_key") not in (None, item["key"]) or keys - {item["key"]}: raise ValueError("item key mismatch")
            if prs and item.get("pr_number") and prs != {item["pr_number"]}: raise ValueError("PR number mismatch")
            if (_has_symlink(path, index)
                    or not re.fullmatch(r"run-[0-9a-f]{32}", path.parent.name)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("card_hash")))
                    or not isinstance(record.get("plan"), dict)
                    or not isinstance(record.get("attempts"), list) or not record["attempts"]
                    or not isinstance(record.get("cwd"), str)
                    or any(type(record.get(k)) not in (int, float) for k in ("started", "ended"))
                    or record["ended"] < record["started"]):
                raise ValueError("spawner fields missing or invalid")
            evidence, text = evidence_from_record(path.parent, record=record)
            if not isinstance(evidence.get("head_sha"), str) or not _HEAD_SHA.fullmatch(evidence["head_sha"]):
                raise ValueError("record completion.head_sha missing or not 40 hex chars")
            if evidence["head_sha"].lower() != head.lower(): raise ValueError("head mismatch")
            family = model_family(evidence.get("served_model"))
            if family is None: raise ValueError("served family unknown")
            if family in writers: raise ValueError("family same as writer")
            ok, _ = parse_verdict(text)
            if ok is None: raise ValueError("no VERDICT")
            if ok and (evidence.get("finish_reason") not in ("stop", "end_turn")
                       or str(evidence.get("test_gaming")).lower() != "no"):
                raise ValueError("PASS safety evidence missing")
            if (path.parent / "evidence.json").exists(): raise ValueError("spawner record shadowed by evidence.json")
            eligible.append(dict(run_id=path.parent.name, family=family, ok=ok,
                                 seat_dir=path.parent, head_sha=evidence["head_sha"]))
        except (OSError, ValueError) as exc:
            completion = record.get("completion")
            reasons.append(f"{exc} [seat={path.parent.name} family={family} head="
                           f"{completion.get('head_sha') if isinstance(completion, dict) else None} expected={head}]")
    return eligible, sorted(set(reasons)) if bound else [
        f"no record [seat=review family=outside:{','.join(writers)} head={head} "
        f"item={item['key']} PR={item.get('pr_number') or 'unresolved'}]"]

def backfill(args, poster, pr_lookup=None):
    """Print TSV discovery and optionally invoke the existing single-seat CLI."""
    import sys
    from collections import Counter
    from plangraph.keeper.evidence_git import GitAdapter
    from seat_verdict import apply_backfill
    apply = getattr(args, "backfill_apply", False)
    if apply and (getattr(args, "dry_run", False) or getattr(args, "backfill_dry_run", False)):
        print("seat_verdict: --backfill-apply must run without --dry-run or --backfill-dry-run", file=sys.stderr)
        return 1
    try:
        config = json.loads((Path.home() / ".local/share/plangraph/keeper.json").read_text())
    except (OSError, ValueError):
        config = {}
    git = GitAdapter(config.get("repos", {}), fetch_interval=float("inf"))
    git._maybe_fetch = lambda repo: False
    try:
        items = json.loads(args.snapshot.read_text("utf-8"))["items"]
        if not isinstance(items, list):
            raise ValueError("snapshot items must be a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"seat_verdict: snapshot: {exc}", file=sys.stderr)
        return 1
    index = record_index(getattr(args, "records_dir", None))
    for reason in getattr(index, "skips", []):
        print(f"seat_verdict: {reason}", file=sys.stderr)
    cache_path = getattr(args, "pr_cache", None)
    try:
        cache = json.loads(cache_path.read_text()) if cache_path else {}
    except (OSError, ValueError):
        cache = {}
    lookup = pr_lookup or ((cache.get if isinstance(cache, dict) else lambda _: None)
                           if cache_path else None)
    if getattr(args, "pr_lookup_gh", False) and apply and lookup is None:
        def lookup(sha):
            result = subprocess.run(["gh", "pr", "list", "--search", sha, "--state", "merged",
                                     "--json", "number"],
                                    capture_output=True, text=True, timeout=30)
            rows = json.loads(result.stdout) if result.returncode == 0 else []
            return rows[0].get("number") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
    output, total, missing, failed, tallies = [], 0, 0, 0, Counter()
    known_keys = {r["key"] for r in items if isinstance(r, dict) and isinstance(r.get("key"), str)}
    for raw in items:
        if not isinstance(raw, dict) or raw.get("state") != "in_review": continue
        item, seats, reasons = dict(raw, key="<missing>", head_sha=None, pr_number=None), [], ["snapshot item missing key"]
        if isinstance(raw.get("key"), str) and raw["key"]:
            item["key"] = raw["key"]
            try:
                item = snapshot_item(raw, index, git)
                item["pr_number"] = merged_pr(item, git, lookup)
            except (KeyError, AttributeError, OSError, subprocess.SubprocessError) as exc:
                reasons = [f"item PR/writer resolution failed: {type(exc).__name__}"]
            else: seats, reasons = eligible_records(item, index, known_keys)
        total += 1
        missing += not bool(seats)
        if not seats: tallies.update({r.split(" [seat=", 1)[0] for r in reasons})
        detail = "ELIGIBLE " + "; ".join(f"{s['run_id']}:{s['family']}:{'PASS' if s['ok'] else 'FIX'}"
                           for s in seats) if seats else "MISSING " + "; ".join(reasons)
        output.append(dict(key=item['key'], status="ELIGIBLE" if seats else "MISSING",
                           head_sha=item.get("head_sha"), pr_number=item.get("pr_number"),
                           delivery=None, verifier_exit=None,
                           seats=[dict(s, seat_dir=str(s['seat_dir'])) for s in seats], reasons=reasons))
        if apply and seats:
            # A terminal CHANGES must never let replay fall through to ACCEPT.
            seat = min(seats, key=lambda s: (s["ok"], str(s["seat_dir"])))
            failed += apply_backfill(args, item, seat, poster, output[-1])
            detail += " delivery=" + output[-1]["delivery"] + " " + "; ".join(output[-1]["reasons"])
        if getattr(args, "format", "tsv") == "tsv":
            print(f"{item['key']}\t{detail.replace(chr(9), ' ').replace(chr(10), ' ')}")
    if getattr(args, "format", "tsv") == "json":
        print(json.dumps(output))
    else:
        print(f"counts\titems={total}\teligible={total - missing}\tmissing={missing}\tfailed={failed}")
        for reason, count in sorted(tallies.items()):
            print(f"reason\t{reason}\t{count}")
    return int(bool(failed))
