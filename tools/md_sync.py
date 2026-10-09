#!/usr/bin/env python3
"""One-way, idempotent import of the router's markdown into the keeper (SYNC-MD-IMPORT v1).

QUESTIONS.md -> open question/operator lanes; ANSWERS.md -> closes them on the cited
decisions; decisions-log.md is checked twice.  The plan pre-checks every cited
decision with the keeper's own parser (``DecisionsAdapter``): a ``Closes:`` list
without the item's key would be refused, so the item gets no ops at all and is
reported as ``close-skipped``.  What is left is one batch (BATCH.md format)
applied through ``apply_batch`` as a single ``apply_ops`` call.

Router D-rows (BY cell names the router, D-505) carry their own ``Closes:``
tokens: ``key@sha`` closes a code item only when the commit is an ancestor of
the keeper config's target ref for that project (``--keeper-config`` /
``PG_CONFIG``, ``repos``); unmerged or unregistered work is planned
``in_review`` instead, and ``--decisions-since N`` ignores D-rows below N.

``--status-file`` leaves one JSON record of the run for the router: ids, counts
and a single error line -- never titles, descriptions or secrets.  It is written
atomically (tmp + rename) on every run, dry or apply, success, refusal or error.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import yaml

import apply_batch
import md_sync_parse as mp
import md_sync_plan as mplan

# md_sync_plan puts src/ on sys.path, which is where the keeper lives.
from plangraph.keeper import config as keeper_config  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402

USAGE = ("usage: md_sync.py --questions P --answers P --decisions P "
         "[--dry-run | --apply] --url U --token-file F [--principal NAME] "
         "[--batch-out FILE] [--status-file FILE] [--keeper-config PATH] "
         "[--decisions-since N]\n")


def write_batch(path, principal, answers_path, digest, ops):
    doc = {"batch": "md-sync", "principal": principal, "source": answers_path,
           "source_sha256": digest, "ops": ops}
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(doc, handle, sort_keys=False, allow_unicode=True)
    return path


def op_key(op):
    return apply_batch.op_key(op)


def status_doc(mode):
    """The status file's shape with a run's counters at zero (fixed key order)."""
    return {"ts": None, "mode": mode, "ops": 0, "applied": False,
            "skipped": 0, "refused": 0, "gone": 0, "redactions": 0,
            "close_skipped": [], "d_closes": [], "d_in_review": [],
            "warnings": [], "models": [], "unmatched": [], "error": None}


def fill_status(status, plan):
    """Counts from a plan: ids and numbers only, so no field text can leak."""
    status["ops"] = len(plan.ops)
    status["skipped"] = len(plan.skipped)
    status["refused"] = len(plan.refused)
    status["gone"] = len(plan.gone)
    status["redactions"] = plan.redactions
    status["close_skipped"] = [
        {"id": name, "decision": dec, "key": key,
         "reason": "Closes does not list the key"}
        for name, dec, key in plan.close_skipped
    ] + [
        {"id": ident, "decision": dec, "key": key, "reason": reason}
        for ident, dec, key, reason in plan.dclose_skipped
    ]
    status["d_closes"] = [{"key": c["key"], "decision": c["decision"]}
                          for c in plan.d_closes]
    status["d_in_review"] = [{"key": r["key"], "decision": r["decision"],
                              "sha": r["sha"], "reason": r["reason"]}
                             for r in plan.d_in_review]
    status["warnings"] = [{"key": w["key"], "decision": w["decision"],
                           "text": w["text"]} for w in plan.warnings]
    status["models"] = [{"key": m["key"], "decision": m["decision"],
                         "text": m["text"]} for m in plan.models]


def write_status(path, status, error, err=None):
    """Record one run's status atomically (tmp + ``os.replace``).

    Readers see the old file or the new one, never half a file; a missing path
    is a no-op, and a failure to write only warns -- it must not change the
    sync's own exit code.
    """
    if not path:
        return
    status["ts"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status["error"] = error
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".md-sync.status.",
                                   dir=os.path.dirname(os.path.abspath(path)) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if err is not None:
            err.write(f"warning: status file not written: {exc}\n")


def _status_flag(argv, default=None):
    """``--status-file`` out of raw argv, for runs argparse rejected."""
    for n, arg in enumerate(argv):
        if arg == "--status-file" and n + 1 < len(argv):
            return argv[n + 1]
        if arg.startswith("--status-file="):
            return arg.split("=", 1)[1]
    return default


def _mode_of(argv):
    """``"apply"`` if argv asked for it, else ``"dry"``."""
    return "apply" if "--apply" in argv else "dry"


class _Tee:
    """A stream that also records what was written (the apply log -> ``error``)."""

    def __init__(self, stream, record):
        self.stream, self.record = stream, record

    def write(self, text):
        self.record.append(text)
        return self.stream.write(text)

    def flush(self):
        return self.stream.flush()


def _apply_error(log):
    """One line naming why the batch died: the apply log's first real line."""
    for text in log.splitlines():
        line = text.strip()
        if line and not line.startswith("acting as "):
            return line
    return "apply failed"


def report(plan, out, dry):
    for n, op in enumerate(plan.ops, 1):
        out.write(f"plan {n} {op['op']} {op_key(op)}\n")
    for ident, why in plan.skipped:
        out.write(f"skipped {ident}: {why}\n")
    for ident, why in plan.refused:
        out.write(f"refused {ident}: {why}\n")
    for ident, decision, key in plan.close_skipped:
        out.write(f"close-skipped {ident}: {decision} Closes does not list {key}\n")
    for key in plan.gone:
        out.write(f"gone {key}: not in QUESTIONS and not answered (not cancelled)\n")
    for line in plan.d_lines:
        out.write(line + "\n")
    for ident, decision, key, reason in plan.dclose_skipped:
        out.write(f"close-skipped {ident}: {decision} {reason} ({key})\n")
    for warn in plan.warnings:
        out.write(f"warning: {warn['text']}\n")
    for model in plan.models:
        out.write(f"models {model['key']}: {model['text']}\n")
    for d_id in plan.invalid_tokens:
        out.write(f"invalid Closes token in {d_id}\n")
    ops = ", ".join(f"{name} {plan.counts[name]}" for name in sorted(plan.counts))
    if dry:
        out.write("dry run: " + (ops or "no ops") + " (no calls made)\n")
    close_skipped = len(plan.close_skipped) + len(plan.dclose_skipped)
    out.write(f"md-sync: ops {len(plan.ops)}, skipped {len(plan.skipped)}, "
              f"refused {len(plan.refused)}, gone {len(plan.gone)}, "
              f"redactions {plan.redactions}, "
              f"close-skipped {close_skipped}, "
              f"d-closes {len(plan.d_closes)}, "
              f"in-review {len(plan.d_in_review)}, "
              f"warnings {len(plan.warnings)}\n")


def main(argv=None, out=None, err=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(add_help=False)
    for name in ("questions", "answers", "decisions", "token-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--url", default=apply_batch.DEFAULT_URL)
    parser.add_argument("--principal", default="router")
    parser.add_argument("--batch-out")
    parser.add_argument("--status-file")
    parser.add_argument("--keeper-config")
    parser.add_argument("--decisions-since", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    try:
        opts = parser.parse_args(argv)
    except SystemExit:
        err.write(USAGE)
        # Even a rejected invocation was a run: record it if the path parsed.
        write_status(_status_flag(argv), status_doc(_mode_of(argv)),
                     "error: bad arguments (see usage)", err)
        return 2
    status = status_doc("apply" if opts.apply else "dry")

    def stop(rc, error):
        """Leave the run's status behind, then hand back the exit code."""
        write_status(opts.status_file, status, error, err)
        return rc

    try:
        texts = {}
        for name in ("questions", "answers", "decisions"):
            with open(getattr(opts, name), "rb") as handle:
                texts[name] = handle.read()
        token = apply_batch.read_token(opts.token_file)
    except (OSError, apply_batch.BatchError) as exc:
        err.write(f"error: {exc}\n")
        return stop(2, f"error: {exc}")
    # D-505: the merge gate reads the keeper config's ``repos`` only (a
    # missing/unreadable config means no registered repo: fail closed there).
    repos = {}
    cfg_path = opts.keeper_config or os.environ.get("PG_CONFIG")
    if cfg_path:
        try:
            repos = keeper_config.load_config(cfg_path).repos
        except keeper_config.ConfigError as exc:
            err.write(f"error: {exc}\n")
            return stop(2, f"error: {exc}")
    digest = hashlib.sha256(texts["answers"]).hexdigest()
    questions = mp.parse_questions(texts["questions"].decode("utf-8", "replace"))
    # QA-IMPORT: rows whose ID cell is not one id never become items: name them (redacted).
    status["unmatched"] = [mplan.credential_guard.mask(mp.redact(cell)[0])[0] for cell in
                           mp.unmatched_rows(texts["questions"].decode("utf-8", "replace"))]
    answers, skipped = mp.parse_answers(texts["answers"].decode("utf-8", "replace"))
    decisions_text = texts["decisions"].decode("utf-8", "replace")
    decisions = mp.parse_decisions(decisions_text)
    session = apply_batch.Session(opts.url, token)
    try:
        items = session.get("snapshot").get("items") or []
    except apply_batch.ApiError as exc:
        err.write(f"error: cannot read the live graph: {exc.code} {exc.msg}\n")
        return stop(2, f"error: cannot read the live graph: {exc.code} {exc.msg}")
    try:
        plan = mplan.build_plan(questions, answers, skipped, decisions, items,
                                decisions_view=DecisionsAdapter(opts.decisions),
                                decisions_text=decisions_text, repos=repos,
                                since=opts.decisions_since)
    except mplan.PlanError as exc:
        out.write(f"error: {exc}\n")
        return stop(1, f"error: {exc}")
    fill_status(status, plan)
    report(plan, out, not opts.apply)
    rc = 1 if plan.refused else 0
    if not opts.apply or not plan.ops:
        return stop(rc, None)
    answers_path = os.path.realpath(opts.answers)
    record = []
    if opts.batch_out:
        path = write_batch(opts.batch_out, opts.principal, answers_path, digest, plan.ops)
        code = _apply(path, opts, _Tee(out, record), _Tee(err, record))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_batch(os.path.join(tmp, "md-sync.yaml"), opts.principal,
                               answers_path, digest, plan.ops)
            code = _apply(path, opts, _Tee(out, record), _Tee(err, record))
    status["applied"] = code == 0
    return stop(code or rc, None if code == 0 else _apply_error("".join(record)))


def _apply(path, opts, out, err):
    return apply_batch.main([path, "--url", opts.url, "--token-file", opts.token_file],
                            out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
