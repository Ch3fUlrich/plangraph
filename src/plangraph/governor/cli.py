"""Command line front-end for keeper metrics upload and start checks."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from plangraph.keeper import cli_verbs
from plangraph.governor import metrics, policy
from plangraph.governor.keeper_source import KeeperSource, KeeperLog

__all__ = ["main", "EXIT_USAGE"]

EXIT_USAGE = 64
ENV_PROJECTS = "PLANGRAPH_CLAUDE_PROJECTS_DIR"
ENV_WEEKLY = "PLANGRAPH_WEEKLY_CAP_WEIGHTED"
ENV_QUOTA = "PLANGRAPH_PROVIDER_QUOTA"
ENV_HOST = "PLANGRAPH_HOST"


def _cmd_daily(args, session) -> int:
    """Compute and (unless dry-run) upload one day's metrics."""
    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        print("bad --date", file=sys.stderr)
        return EXIT_USAGE
    projects_dir = args.projects_dir or os.environ.get(ENV_PROJECTS)
    if not projects_dir:
        print("projects dir is required", file=sys.stderr)
        return EXIT_USAGE

    weekly_cap = args.weekly_cap
    if weekly_cap is None:
        raw = os.environ.get(ENV_WEEKLY)
        weekly_cap = float(raw) if raw else None

    landed = {}
    if args.repo:
        landed = metrics.landed_by_day(
            args.repo, day.isoformat(),
            until=(day + timedelta(days=1)).isoformat(), ref=args.ref)

    try:
        records = metrics.iter_usage(projects_dir)
        # a second pass: the ctx report counts the FIRST usage record per
        # message id, which iter_usage's largest-wins dedup has already dropped
        ctx_records = metrics.iter_usage_first(projects_dir)
    except OSError as exc:
        print(f"cannot read projects dir: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        session_classes = (metrics.load_session_classes(args.session_classes)
                           if args.session_classes else None)
    except (OSError, ValueError) as exc:
        print(f"bad session-classes file: {exc}", file=sys.stderr)
        return EXIT_USAGE
    host = args.host or os.environ.get(ENV_HOST)
    metrics.tag_records(records, host=host, session_classes=session_classes)
    metrics.tag_records(ctx_records, host=host, session_classes=session_classes)

    items = list(metrics.daily_metrics(
        day.isoformat(), records, landed, weekly_cap))
    ctx = metrics.ctx_report(ctx_records, day.isoformat())

    if args.provider_usage:
        try:
            with open(args.provider_usage, encoding="utf-8") as fh:
                usage = json.load(fh)
            for provider, tokens in usage.items():
                items.append({"metric": f"provider_used.{provider}",
                              "value": tokens, "source": "provider-usage"})
        except (OSError, ValueError) as exc:
            print(f"bad provider-usage file: {exc}", file=sys.stderr)
            return EXIT_USAGE

    if args.rate_limited:
        for provider in (p.strip() for p in args.rate_limited.split(",")):
            if provider:
                items.append({"metric": f"rate_limited.{provider}",
                              "value": 1.0, "source": "operator"})

    posted = False
    if not args.dry_run:
        try:
            session.call("POST", "set_metrics",
                         body={"day": day.isoformat(), "items": items})
            posted = True
        except cli_verbs.Unreachable:
            return 3
        except cli_verbs.RefusedCli as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2

    # the ctx report is a file artefact too, not only a stdout section:
    # DIR/ctx-<day>.json, DIR = --out-dir else the projects dir the job read
    out_dir = args.out_dir or projects_dir
    ctx_path = os.path.join(os.fspath(out_dir), f"ctx-{day.isoformat()}.json")
    try:
        os.makedirs(out_dir, exist_ok=True)
        _write_ctx(ctx_path, ctx)
    except OSError as exc:
        print(f"cannot write ctx report {ctx_path}: {exc}", file=sys.stderr)

    print(json.dumps({"day": day.isoformat(), "items": items,
                      "posted": posted, "ctx": ctx}))
    return 0


def _write_ctx(path: str, payload: dict) -> None:
    """Write the ctx report as indented JSON, replacing the old one atomically."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _cmd_check(args, session) -> int:
    """Run one start check and print the verdict."""
    try:
        now = datetime.fromisoformat(args.now) if args.now \
            else datetime.now(timezone.utc)
    except ValueError:
        print("bad --now", file=sys.stderr)
        return EXIT_USAGE

    quota_text = args.quota if args.quota is not None \
        else os.environ.get(ENV_QUOTA)
    try:
        quotas = policy.parse_quota(quota_text)
    except (ValueError, TypeError):
        print("bad --quota", file=sys.stderr)
        return EXIT_USAGE

    fallbacks = [f.strip() for f in (args.fallback or "").split(",")
                 if f.strip()]

    source = KeeperSource(session)
    log = KeeperLog(session)
    try:
        decision = policy.allow(
            args.start_kind, args.initiator, now,
            source=source, quotas=quotas, provider=args.provider,
            override=args.override, on_override=log,
            fallbacks=fallbacks, on_fallback=log)
    except (ValueError, TypeError):
        print("bad start_kind/initiator/quota", file=sys.stderr)
        return EXIT_USAGE

    print(json.dumps({"allow": decision.allow, "reason": decision.reason,
                      "override": decision.override,
                      "fallback": decision.fallback,
                      "provider": decision.provider,
                      "burn": decision.burn}))
    return 0 if decision.allow else 1


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse tree."""
    parser = argparse.ArgumentParser(prog="plangraph-governor")
    sub = parser.add_subparsers(dest="command", required=True)

    metrics_p = sub.add_parser("metrics")
    metrics_sub = metrics_p.add_subparsers(dest="metrics_command",
                                           required=True)
    daily = metrics_sub.add_parser("daily")
    today = datetime.now(timezone.utc).date()
    daily.add_argument("--date",
                       default=(today - timedelta(days=1)).isoformat())
    daily.add_argument("--projects-dir", default=None)
    daily.add_argument("--repo", default=None)
    daily.add_argument("--ref", default="HEAD")
    daily.add_argument("--weekly-cap", type=float, default=None)
    daily.add_argument("--host", default=None,
                       help="host name for the ctx report "
                            f"(default: ${ENV_HOST} or the local hostname)")
    daily.add_argument("--session-classes", default=None, metavar="PATH",
                       help="JSON {session_id: class} launch-record mapping")
    daily.add_argument("--provider-usage", default=None)
    daily.add_argument("--rate-limited", default=None)
    daily.add_argument("--out-dir", default=None, metavar="DIR",
                       help="write the ctx report to DIR/ctx-<date>.json "
                            "(default: the projects dir)")
    daily.add_argument("--dry-run", action="store_true")

    check = sub.add_parser("check")
    check.add_argument("start_kind")
    check.add_argument("initiator")
    check.add_argument("--provider", default=None)
    check.add_argument("--fallback", default=None)
    check.add_argument("--quota", default=None)
    check.add_argument("--override", default=None)
    check.add_argument("--now", default=None)
    return parser


def main(argv=None, session=None) -> int:
    """Entry point; ``session`` is injectable for tests."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if session is None:
        session = cli_verbs.Session()
    if args.command == "metrics" and args.metrics_command == "daily":
        return _cmd_daily(args, session)
    if args.command == "check":
        return _cmd_check(args, session)
    return EXIT_USAGE
