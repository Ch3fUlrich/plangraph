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
from pathlib import Path

_HEAD_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
# F8: both spellings count (review/tail.md uses TEST-GAMING).
_TEST_GAMING = re.compile(r"^TEST[_-]GAMING:[ \t]*(.*)$", re.I | re.M)


def _verdict_line():
    """seat_verdict's authoritative VERDICT-line grammar (lazy: circular import)."""
    from seat_verdict import _VERDICT_LINE
    return _VERDICT_LINE


def _canon_family(name):
    """Canonical family of a model or family name via seat_verdict's alias table."""
    from seat_verdict import model_family, KNOWN_FAMILIES  # local: seat_verdict imports this module
    family = model_family(name)
    if family is not None:
        return family
    candidate = str(name or "").strip().lower()
    if candidate not in KNOWN_FAMILIES:
        raise ValueError(f"unknown model family {name!r}")
    return candidate


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
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ValueError(f"record.json is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError("record.json is not an object")
    try:
        stat = record_path.stat()
    except OSError as exc:
        raise ValueError(f"record.json unreadable: {type(exc).__name__}") from exc
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
    verdict_line = _verdict_line()
    lines = review_text.splitlines()
    verdicts = {" ".join(m.group(1).split()).lower()
                for line in lines
                for m in [verdict_line.match(line)] if m}
    if len(verdicts) > 1:
        raise ValueError("conflicting VERDICT lines")
    last = max((i for i, line in enumerate(lines) if verdict_line.match(line)),
               default=-1)
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


def record_paths(records_dir):
    """Include host-partitioned spawner records as well as local runs."""
    return sorted(Path(records_dir).glob("**/run-*/record.json"))


def record_index(records_dir):
    """Read each record and prompt once, shared by writer and review discovery."""
    if isinstance(records_dir, dict):
        return records_dir
    rows = {}
    for path in record_paths(records_dir):
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
    import subprocess
    from types import SimpleNamespace
    from plangraph.keeper.evidence_repo import repo_key
    head = item.get("head_sha")
    if not head:
        return None
    if lookup is not None:
        return lookup(head)
    key = repo_key(item, SimpleNamespace(git=git), head)
    repo = git.repo_path(key)
    item["repo_path"] = repo
    if repo:
        result = subprocess.run(["git", "show", "-s", "--format=%s", head],
                                cwd=repo, capture_output=True, text=True)
        match = re.search(r"Merge PR #(\d+)\b", result.stdout)
        if result.returncode == 0 and match:
            return int(match[1])
    return None


def writer_families(item, head, records_dir, git):
    """Prefer evidenced git writers; fall back to matching implement records."""
    from types import SimpleNamespace
    from subprocess import SubprocessError
    from plangraph.keeper.evidence_repo import repo_key
    from seat_verdict import model_family
    models = []
    if git is not None and head:
        key = repo_key(item, SimpleNamespace(git=git), head)
        repo = git.repo_path(key)
        if repo:
            try:
                rows = git.writer_trailers(repo, head, git.repo_ref(key))
            except (OSError, SubprocessError):
                rows = []
            for trailers in rows:
                for name, value in trailers.items():
                    if name.lower() == "writer":
                        models.extend(v.split()[0] for v in
                                      (value if isinstance(value, list) else [value]) if v.split())
    if not models and records_dir is not None:
        for path, (r, _) in record_index(records_dir).items():
            try:
                if (r.get("role") == "implement" and r.get("item_key") == item["key"]
                        and not any(p.is_symlink() for p in (path, *path.absolute().parents))):
                    models.append(r.get("served_model"))
            except (OSError, ValueError, AttributeError):
                continue
    families = [model_family(m) for m in models]
    return sorted(set(families)) if families and None not in families else []


def snapshot_item(item, records_dir=None, git=None):
    """Choose newest verified evidence; conflicting SHAs in its epoch refuse."""
    rows = []
    for e in item.get("evidence", []):
        if e.get("kind") not in ("commit", "commit+ci") or e.get("verified") is not True:
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


def eligible_records(item, records_dir):
    """Pure local discovery: return (eligible records, rejection reasons).

    Spawner schema/ownership checks are structural, not cryptographic proof:
    a malicious same-uid writer can fabricate a complete spawner record.
    No tokens, network, filesystem writes, or keeper mutations occur here.
    """
    from seat_verdict import KNOWN_FAMILIES, model_family, parse_verdict
    head, writers = item.get("head_sha"), item.get("writer_families")
    if not isinstance(head, str) or not _HEAD_SHA.fullmatch(head):
        return [], [item.get("missing_reason") or "head mismatch: missing or ambiguous evidenced commit"]
    if not writers or any(w not in KNOWN_FAMILIES for w in writers):
        return [], ["unknown writer"]
    eligible, reasons = [], []
    for path, (record, prompt) in record_index(records_dir).items():
        try:
            if record.get("role") != "review":
                continue
            prs = {int(n) for n in re.findall(r"\bPR\s+#(\d+)\b", prompt, re.I)}
            named = bool(re.search(r"(?<![\w:])" + re.escape(item["key"]) + r"(?![\w-])", prompt))
            same_pr = item.get("pr_number") in prs
            if record.get("item_key") != item["key"] and not (named or same_pr):
                continue
            if prs and item.get("pr_number") and prs != {item["pr_number"]}:
                raise ValueError("PR number mismatch")
            if (any(p.is_symlink() for p in (path, *path.absolute().parents))
                    or not re.fullmatch(r"run-[0-9a-f]{32}", path.parent.name)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("card_hash")))
                    or not isinstance(record.get("plan"), dict)
                    or not isinstance(record.get("attempts"), list)
                    or not record["attempts"]
                    or not isinstance(record.get("cwd"), str)
                    or any(type(record.get(k)) not in (int, float) for k in ("started", "ended"))
                    or record["ended"] < record["started"]):
                raise ValueError("spawner fields missing or invalid")
            evidence, text = evidence_from_record(path.parent, record=record)
            if not isinstance(evidence.get("head_sha"), str) or not _HEAD_SHA.fullmatch(evidence["head_sha"]):
                raise ValueError("record completion.head_sha missing or invalid")
            from seat_verdict import card_head_matches
            if evidence["head_sha"].lower() != head.lower() and not card_head_matches(
                    path.parent, evidence["head_sha"], head, item.get("repo_path")):
                raise ValueError("head mismatch")
            family = model_family(record.get("served_model"))
            if family is None:
                raise ValueError("served family unknown")
            if family in writers:
                raise ValueError("family same as writer")
            ok, _ = parse_verdict(text)
            if ok is None:
                raise ValueError("no VERDICT")
            if ok and (evidence.get("finish_reason") not in ("stop", "end_turn")
                       or str(evidence.get("test_gaming")).lower() != "no"):
                raise ValueError("PASS safety evidence missing")
            if (path.parent / "evidence.json").exists():
                raise ValueError("spawner record shadowed by evidence.json")
            eligible.append(dict(run_id=path.parent.name, family=family, ok=ok,
                                 seat_dir=path.parent, head_sha=evidence["head_sha"]))
        except (OSError, ValueError) as exc:
            completion = record.get("completion")
            reasons.append(f"{exc} [seat={path.parent.name} family="
                           f"{model_family(record.get('served_model'))} head="
                           f"{completion.get('head_sha') if isinstance(completion, dict) else None} expected={head}]")
    return eligible, sorted(set(reasons)) or ([] if eligible else [
        f"no record [seat=review family=outside:{','.join(writers)} head={head} "
        f"item={item['key']} PR={item.get('pr_number') or 'unresolved'}]"])


def backfill(args, poster, pr_lookup=None):
    """Print TSV discovery and optionally invoke the existing single-seat CLI."""
    import sys
    from collections import Counter
    from plangraph.keeper.evidence_git import GitAdapter
    try:
        config = json.loads((Path.home() / ".local/share/plangraph/keeper.json").read_text())
    except (OSError, ValueError):
        config = {}
    git = GitAdapter(config.get("repos", {}), fetch_interval=float("inf"))
    # Discovery must never fetch or mutate repositories.
    git._maybe_fetch = lambda repo: False
    try:
        snapshot = json.loads(args.snapshot.read_text("utf-8"))
        items = snapshot["items"]
        if not isinstance(items, list):
            raise ValueError("snapshot items must be a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"seat_verdict: snapshot: {exc}", file=sys.stderr)
        return 1
    index = record_index(args.records_dir)
    cache_path = getattr(args, "pr_cache", None)
    cache = json.loads(cache_path.read_text()) if cache_path else None
    lookup = pr_lookup or ((lambda sha: cache.get(sha)) if cache is not None else None)
    if getattr(args, "pr_lookup_gh", False) and lookup is None:
        import subprocess
        def lookup(sha):
            result = subprocess.run(["gh", "pr", "list", "--search", sha, "--state", "merged",
                                     "--json", "number"],
                                    capture_output=True, text=True, timeout=30)
            rows = json.loads(result.stdout) if result.returncode == 0 else []
            return rows[0]["number"] if len(rows) == 1 else None
    output = []
    total = missing = failed = 0
    tallies = Counter()
    for raw in items:
        if raw.get("state") != "in_review":
            continue
        item = snapshot_item(raw, index, git)
        item = dict(item, pr_number=merged_pr(item, git, lookup))
        seats, reasons = eligible_records(item, index)
        total += 1
        missing += not bool(seats)
        if not seats:
            tallies.update({r.split(" [seat=", 1)[0] for r in reasons})
        detail = "ELIGIBLE " + "; ".join(f"{s['run_id']}:{s['family']}:{'PASS' if s['ok'] else 'FIX'}"
                           for s in seats) if seats else "MISSING " + "; ".join(reasons)
        output.append(dict(key=item['key'], status="ELIGIBLE" if seats else "MISSING",
                           head_sha=item.get("head_sha"), pr_number=item.get("pr_number"),
                           seats=[dict(s, seat_dir=str(s['seat_dir'])) for s in seats], reasons=reasons))
        if getattr(args, "format", "tsv") == "tsv":
            print(f"{item['key']}\t{detail.replace(chr(9), ' ').replace(chr(10), ' ')}")
        if args.backfill_apply and seats:
            # One deterministic record per item; never silently post all runs.
            seat = seats[0]
            failed += bool(poster(["--key", item["key"], "--seat-dir", str(seat["seat_dir"]),
                                    "--head", seat["head_sha"], "--writer-family",
                                   ",".join(item["writer_families"]), "--token-dir",
                                   str(args.token_dir), "--url", args.url]))
    if getattr(args, "format", "tsv") == "json":
        print(json.dumps(output))
        return int(bool(failed))
    print(f"counts\titems={total}\teligible={total - missing}\tmissing={missing}\tfailed={failed}")
    for reason, count in sorted(tallies.items()):
        print(f"reason\t{reason}\t{count}")
    return int(bool(failed))
