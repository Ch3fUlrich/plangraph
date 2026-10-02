#!/usr/bin/env python3
"""One-way, idempotent import of the router's markdown into the keeper (SYNC-MD-IMPORT v1).

QUESTIONS.md -> open question/operator lanes; ANSWERS.md -> closes them on the cited
decisions; decisions-log.md is only checked.  The result is one batch (BATCH.md format)
applied through ``apply_batch`` as a single ``apply_ops`` call.
"""
import argparse
import hashlib
import os
import sys
import tempfile

import yaml

import apply_batch
import md_sync_parse as mp
import md_sync_plan as mplan

USAGE = ("usage: md_sync.py --questions P --answers P --decisions P "
         "[--dry-run | --apply] --url U --token-file F [--principal NAME] "
         "[--batch-out FILE]\n")


def write_batch(path, principal, answers_path, digest, ops):
    doc = {"batch": "md-sync", "principal": principal, "source": answers_path,
           "source_sha256": digest, "ops": ops}
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(doc, handle, sort_keys=False, allow_unicode=True)
    return path


def op_key(op):
    return apply_batch.op_key(op)


def report(plan, out, dry):
    for n, op in enumerate(plan.ops, 1):
        out.write(f"plan {n} {op['op']} {op_key(op)}\n")
    for ident, why in plan.skipped:
        out.write(f"skipped {ident}: {why}\n")
    for ident, why in plan.refused:
        out.write(f"refused {ident}: {why}\n")
    for key in plan.gone:
        out.write(f"gone {key}: not in QUESTIONS and not answered (not cancelled)\n")
    ops = ", ".join(f"{name} {plan.counts[name]}" for name in sorted(plan.counts))
    if dry:
        out.write("dry run: " + (ops or "no ops") + " (no calls made)\n")
    out.write(f"md-sync: ops {len(plan.ops)}, skipped {len(plan.skipped)}, "
              f"refused {len(plan.refused)}, gone {len(plan.gone)}, "
              f"redactions {plan.redactions}\n")


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(add_help=False)
    for name in ("questions", "answers", "decisions", "token-file"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--url", default=apply_batch.DEFAULT_URL)
    parser.add_argument("--principal", default="router")
    parser.add_argument("--batch-out")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    try:
        opts = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except SystemExit:
        err.write(USAGE)
        return 2
    try:
        texts = {}
        for name in ("questions", "answers", "decisions"):
            with open(getattr(opts, name), "rb") as handle:
                texts[name] = handle.read()
        token = apply_batch.read_token(opts.token_file)
    except (OSError, apply_batch.BatchError) as exc:
        err.write(f"error: {exc}\n")
        return 2
    digest = hashlib.sha256(texts["answers"]).hexdigest()
    questions = mp.parse_questions(texts["questions"].decode("utf-8", "replace"))
    answers, skipped = mp.parse_answers(texts["answers"].decode("utf-8", "replace"))
    decisions = mp.parse_decisions(texts["decisions"].decode("utf-8", "replace"))
    session = apply_batch.Session(opts.url, token)
    try:
        items = session.get("snapshot").get("items") or []
    except apply_batch.ApiError as exc:
        err.write(f"error: cannot read the live graph: {exc.code} {exc.msg}\n")
        return 2
    try:
        plan = mplan.build_plan(questions, answers, skipped, decisions, items)
    except mplan.PlanError as exc:
        out.write(f"error: {exc}\n")
        return 1
    report(plan, out, not opts.apply)
    rc = 1 if plan.refused else 0
    if not opts.apply or not plan.ops:
        return rc
    answers_path = os.path.realpath(opts.answers)
    if opts.batch_out:
        path = write_batch(opts.batch_out, opts.principal, answers_path, digest, plan.ops)
        return _apply(path, opts, out, err) or rc
    with tempfile.TemporaryDirectory() as tmp:
        path = write_batch(os.path.join(tmp, "md-sync.yaml"), opts.principal,
                           answers_path, digest, plan.ops)
        return _apply(path, opts, out, err) or rc


def _apply(path, opts, out, err):
    return apply_batch.main([path, "--url", opts.url, "--token-file", opts.token_file],
                            out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
