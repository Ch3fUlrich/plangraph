"""Historical seat batch orchestration and shared-outbox delivery helpers.

Discovery stays in seat_record; the supplied single-seat poster remains the
only path to verdict verification and keeper POSTs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def apply_backfill(args, item, seat, poster, row, *, retry_reason=None):
    """Use the forward outbox lock without waiting; ambiguous state never sends."""
    from contextlib import redirect_stdout, redirect_stderr
    from io import StringIO
    from plangraph.spawner.notify import _verdict_outcome
    from plangraph.spawner.private_io import atomic_write_private
    directory, row["delivery"], row["verifier_exit"] = seat["seat_dir"], "skipped", None
    url = getattr(args, "url", os.environ.get("PG_URL"))
    if not url or not all(getattr(args, k, None) for k in ("backfill_apply", "token_dir")) or any(
            getattr(args, k, False) for k in ("dry_run", "backfill_dry_run")):
        row["delivery"] = "failed"
        row["reasons"].append("backfill requires explicit --backfill-apply without dry-run; apply requires --url and --token-dir")
        return 1
    journal = directory / "verdict.json"
    phase = "verdict outbox check"
    try:
        if journal.is_symlink() or Path(str(journal) + ".lock").is_symlink():
            row["reasons"].append("ambiguous verdict journal")
            return 0
        phase = "verdict lock open"
        with os.fdopen(os.open(str(journal) + ".lock", os.O_RDWR | os.O_CREAT, 0o600), "r+b") as lock:
            phase = "verdict lock"
            if os.name == "nt":
                import msvcrt
                lock.write(b"0")
                lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            phase = "verdict journal read"
            previous = json.loads(journal.read_text()) if journal.exists() else {}
            if (not isinstance(previous, dict) or journal.exists() and previous.get("status") not in
                    ("sending", "delivered", "skipped", "retry")
                    or type(previous.get("attempts", 0)) is not int or previous.get("attempts", 0) < 0):
                raise ValueError("ambiguous verdict journal")
            if ((directory / "verdict.delivered").exists()
                    or previous.get("status") in ("sending", "delivered", "skipped")):
                row["reasons"].append("verdict already terminal or sending")
                return 0
            if retry_reason is not None and (previous.get("status") != "retry"
                                            or previous.get("reason") != retry_reason):
                raise ValueError("not a transport-only retry")
            phase = "verdict sending journal write"
            atomic_write_private(journal, json.dumps(dict(status="sending",
                attempts=previous.get("attempts", 0))) + "\n")
            phase = "verdict poster or verifier result"
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                rc = poster(["--key", item["key"], "--seat-dir", str(directory), "--head", seat["head_sha"],
                             "--writer-family", ",".join(item["writer_families"]), "--token-dir",
                              str(args.token_dir), "--url", url])
            if type(rc) is not int or rc < 0: raise ValueError("invalid verifier exit")
            row["verifier_exit"] = rc
            phase = "verdict outcome write"
            # These exits precede token reads/send; only an explicit transport
            # retry may retain its prior authorization after a verifier refusal.
            if retry_reason is not None and rc in (2, 3, 4):
                atomic_write_private(journal, json.dumps(previous) + "\n")
                row["reasons"].append("repost verifier refused before send: " + str(rc))
            else:
                _verdict_outcome(directory, "delivered" if rc == 0 else "skipped", "backfill verifier exit " + str(rc))
            row["delivery"] = "failed" if rc else "delivered"
            return int(bool(rc))
    except Exception as exc:
        if phase == "verdict lock" and isinstance(exc, BlockingIOError):
            row["reasons"].append("verdict lock busy")
            return 0
        if phase == "verdict journal read" and isinstance(exc, ValueError):
            row["reasons"].append("ambiguous verdict journal")
            return 0
        row["delivery"] = "failed"
        row["reasons"].append(f"{phase} failed: {type(exc).__name__}")
        return 1


def repost_transport(args, poster):
    """Explicit operator repost, sharing the outbox lock and all verifier gates."""
    from types import SimpleNamespace
    row = {"reasons": []}
    try:
        if (args.seat_dir / "evidence.json").exists() or args.dry_run:
            raise ValueError("transport repost requires a record seat without --dry-run")
        record = json.loads((args.seat_dir / "record.json").read_text("utf-8"))
        if isinstance(record, dict) and "item_key" in record and record["item_key"] != args.key:
            raise ValueError("record item_key does not match --key")
        writers = record.get("given_writer_families") if isinstance(record, dict) else None
        if not isinstance(writers, list) or not writers or any(not isinstance(w, str) for w in writers):
            raise ValueError("transport repost requires recorded given_writer_families or explicit --writer-family")
        completion = record.get("completion")
        recorded_head = completion.get("head_sha") if isinstance(completion, dict) else None
        if args.head and recorded_head and args.head.strip().lower() != str(recorded_head).lower():
            raise ValueError("record completion.head_sha does not match --head")
        head = args.head or recorded_head
        if not isinstance(head, str) or not head:
            raise ValueError("transport repost requires completion.head_sha or --head")
        apply_backfill(SimpleNamespace(backfill_apply=True, url=args.url, token_dir=args.token_dir),
                       {"key": args.key, "writer_families": writers},
                       {"seat_dir": args.seat_dir, "head_sha": head}, poster, row,
                       retry_reason="keeper verdict transport not configured")
    except (Exception, SystemExit) as exc:
        row["reasons"].append(str(exc))
    if row.get("delivery") != "delivered":
        print("seat_verdict: repost refused: " + "; ".join(row["reasons"]), file=sys.stderr)
        return 1
    print(f"key={args.key} repost=delivered")
    return 0


def backfill(args, poster, pr_lookup=None):
    """Print TSV discovery and optionally invoke the existing single-seat CLI."""
    from collections import Counter
    from plangraph.keeper.evidence_git import GitAdapter
    from seat_record import record_index, snapshot_item, merged_pr, eligible_records
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
