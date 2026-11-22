"""Historical seat batch orchestration and shared-outbox delivery helpers.

Discovery stays in seat_record; the supplied single-seat poster remains the
only path to verdict verification and keeper POSTs.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path

_SEND_STATE = ContextVar("seat_verdict_send_state", default=None)
EXIT_IO, EXIT_REFUSED = 1, 2


def _add_src():
    src = str(Path(__file__).resolve().parents[1] / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
        return src
    return None


def mark_verdict_recovery(directory, reason):
    """Expose an unproven historical verdict to routing without sending it."""
    added = _add_src()
    try:
        from plangraph.spawner.private_io import atomic_write_private
        atomic_write_private(directory / "verdict.recovery.json",
                             json.dumps({"reason": reason}) + "\n")
    finally:
        if added is not None:
            sys.path.remove(added)


def save_verdict_payload(directory, family, payload):
    """Pin the verified decision before contacting the keeper."""
    added = _add_src()
    try:
        from plangraph.spawner.notify import inbox_lock
        from plangraph.spawner.private_io import atomic_write_private

        path = directory / "verdict.payload.json"
        with inbox_lock(path):
            if not path.exists():
                atomic_write_private(path, json.dumps({"family": family, "payload": payload}) + "\n")
            return read_verdict_payload(directory)
    finally:
        if added is not None:
            sys.path.remove(added)


def read_verdict_payload(directory):
    path = directory / "verdict.payload.json"
    if path.is_symlink():
        raise ValueError("verdict payload path is a symlink")
    return json.loads(path.read_text("utf-8"))


def post_verdict(url, route, token, payload, timeout):
    request = urllib.request.Request(
        url.rstrip("/") + route, data=json.dumps(payload).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")


def dispatch_verdict(key, url, token_dir, decision, send, known_families):
    family, payload = decision["family"], decision["payload"]
    if family not in known_families or payload.get("key") != key or not payload.get("binding"):
        return EXIT_REFUSED
    state = _SEND_STATE.get()
    try:
        token = (token_dir / f"reviewer-{family}.token").read_text("utf-8").strip()
        if not token:
            raise ValueError("empty reviewer token")
        if state is not None:
            state["started"] = True
        status, reply = send(url, token, payload)
    except (OSError, ValueError) as exc:
        if state is not None:
            state["started"] = False
        print(f"seat_verdict: send failed: {type(exc).__name__}", file=sys.stderr)
        return EXIT_IO
    except BaseException:
        if state is not None:
            state["started"] = False
        raise
    short = " ".join(reply.split())[:120]
    print(f"key={key} family={family} ok={payload['ok']} http={status} reply={short}", flush=True)
    return 0 if 200 <= status < 300 else EXIT_REFUSED if status in (400, 401, 403, 409) else EXIT_IO


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
        if retry_reason is not None and (not re.fullmatch(r"[0-9a-fA-F]{40}", seat["head_sha"] or "")
                or not isinstance(item["writer_families"], list) or not item["writer_families"] or any(
                    not isinstance(w, str) or not w.strip() or w.lstrip().startswith("-") for w in item["writer_families"])):
            raise ValueError("invalid repost head or writer bindings")
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
            previous_text = journal.read_text() if journal.exists() else "{}"
            previous = json.loads(previous_text)
            if (not isinstance(previous, dict) or journal.exists() and previous.get("status") not in
                    ("sending", "delivered", "skipped", "retry")
                    or type(previous.get("attempts", 0)) is not int or previous.get("attempts", 0) < 0):
                raise ValueError("ambiguous verdict journal")
            bound = (directory / "verdict.payload.json").is_file()
            if previous.get("status") == "sending" and not bound:
                mark_verdict_recovery(directory, "legacy sending entry has no bound payload")
            if ((directory / "verdict.delivered").exists()
                    or previous.get("status") in ("delivered", "skipped")
                    or previous.get("status") == "sending" and not bound):
                row["reasons"].append("verdict already terminal or sending")
                return 0
            if retry_reason is not None and not (bound and previous.get("status") in ("sending", "retry")) \
                    and (previous.get("status") != "retry" or previous.get("reason") != retry_reason):
                row["delivery"] = "failed"
                row["reasons"].append("journal is not a transport-only retry")
                return 1
            phase = "verdict sending journal write"
            atomic_write_private(journal, json.dumps(dict(status="sending",
                attempts=previous.get("attempts", 0))) + "\n")
            phase = "verdict poster or verifier result"
            context = _SEND_STATE.set(state := {})
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    rc = poster(["--key", item["key"], "--seat-dir", str(directory), "--head", seat["head_sha"],
                                 "--writer-family", ",".join(item["writer_families"]), "--token-dir",
                                  str(args.token_dir), "--url", url])
            finally:
                _SEND_STATE.reset(context)
                if retry_reason is not None and state.get("started") is False:
                    atomic_write_private(journal, previous_text)
            if type(rc) is not int or rc < 0: raise ValueError("invalid verifier exit")
            row["verifier_exit"] = rc
            if (directory / "verdict.recovery.json").is_file():
                row["reasons"].append("routing recovery required; see verdict.recovery.json")
            if rc and retry_reason is not None: row["reasons"].append(f"poster exit {rc}")
            phase = "verdict outcome write"
            if retry_reason is None or state.get("started") is not False:
                retry = rc == EXIT_IO and (directory / "verdict.payload.json").is_file()
                _verdict_outcome(directory, "delivered" if rc == 0 else "retry" if retry else "skipped",
                                 "backfill verifier exit " + str(rc))
            row["delivery"] = "failed" if rc else "delivered"
            return int(bool(rc))
    except (Exception, SystemExit) as exc:
        if retry_reason is None and isinstance(exc, SystemExit): raise
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
    """Recheck a record through the verifier under the shared outbox lock."""
    row = {"reasons": []}
    added_src = _add_src() if "plangraph" not in sys.modules else None
    try:
        if (args.seat_dir / "evidence.json").exists():
            print("seat_verdict: repost refused: evidence.json exists; record-only seat required", file=sys.stderr)
            return 1
        if args.dry_run: raise ValueError("record seat without --dry-run required")
        record = json.loads((args.seat_dir / "record.json").read_text("utf-8"))
        args.backfill_apply = True
        apply_backfill(args, {"key": args.key, "writer_families": record.get("given_writer_families")},
                       {"seat_dir": args.seat_dir, "head_sha": args.head or record.get("completion", {}).get("head_sha")}, poster, row,
                       retry_reason="keeper verdict transport not configured")
    except (Exception, SystemExit) as exc:
        row["reasons"].append(type(exc).__name__)
    finally:
        if added_src is not None:
            sys.path.remove(added_src)
    if row.get("delivery") != "delivered": print("seat_verdict: repost refused: " + "; ".join(row["reasons"]), file=sys.stderr)
    return int(row.get("delivery") != "delivered")


def backfill(args, poster, pr_lookup=None):
    """Print TSV discovery and optionally invoke the existing single-seat CLI."""
    _add_src()
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
