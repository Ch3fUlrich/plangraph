#!/usr/bin/env python3
"""Read-only Claude transcript accounting. Windows are [since, until), UTC.

Only compact request statistics and matched key sets survive parsing; transcript
text is never printed or retained. Session identity is the parent file identity.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
import re
import statistics
import sys

UNSEEN = "cannot see: the Claude meter, billed money, other hosts' transcripts"
FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
          "cache_read_input_tokens")


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def load_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def lines(path):
    with Path(path).open(encoding="utf-8") as handle:
        result = {line.strip() for line in handle if line.strip()}
    if not result:
        raise ValueError("empty input")
    return result


def canonical(key):
    return key.rsplit(":", 1)[-1]


def content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(content(v) for v in value)
    if isinstance(value, dict):
        return " ".join(content(v) for k, v in value.items()
                        if k in ("text", "content", "input") or "type" not in value)
    return ""


def matches(record, patterns):
    text = content(record.get("message", {}).get("content", record.get("content", "")))
    return {key for key, pattern in patterns.items() if pattern.search(text)}


def read_projects(root, patterns, stats):
    requests, sessions, seen_files = {}, {}, set()
    paths = sorted(root.glob("*/*.jsonl"))
    for parent in paths:
        sid = str(parent.resolve())
        session = {"id": parent.stem, "role": "unnamed", "start": None}
        sessions[sid] = session
        files = [parent] + sorted((parent.parent / parent.stem / "subagents").glob("agent-*.jsonl"))
        for path in files:
            identity = path.resolve()
            if identity in seen_files:
                continue
            seen_files.add(identity)
            pending = set()
            with path.open(encoding="utf-8") as handle:
                nonempty = False
                for index, raw in enumerate(handle):
                    nonempty = True
                    try:
                        record = json.loads(raw)
                        if not isinstance(record, dict):
                            raise ValueError()
                        if path == parent and index == 0 and record.get("timestamp"):
                            session["start"] = instant(record["timestamp"])
                        kind = record.get("type")
                        if path == parent and kind in ("custom-title", "agent-name"):
                            name = record.get("customTitle") or record.get("agentName")
                            if name:
                                session["role"] = re.sub(r"-\d+$", "", name)
                        found = matches(record, patterns)
                        if kind != "assistant":
                            if kind in ("user", "tool_result"):
                                pending |= found
                            continue
                        message = record["message"]
                        mid = message["id"]
                        key = (sid, mid)
                        usage = message["usage"]
                        counts = [float(usage.get(field, 0)) for field in FIELDS]
                        if any(n < 0 or not math.isfinite(n) for n in counts):
                            raise ValueError()
                        # Repeated content-block records replace usage, but all
                        # blocks' key mentions and the initiating user belong here.
                        old = requests.get(key)
                        named = found | pending | (old["keys"] if old else set())
                        requests[key] = dict(session=sid, time=instant(record["timestamp"]),
                                             model=message.get("model", "unknown"),
                                             counts=counts, keys=named,
                                             subagent=bool(record.get("isSidechain")) or path != parent)
                        pending = set()
                    except (ValueError, KeyError, TypeError, AttributeError):
                        stats["malformed_lines"] += 1
                if not nonempty:
                    raise ValueError("empty transcript")
    return list(requests.values()), sessions


def weighted(request, policy):
    return sum(n * policy["weights"][f] for n, f in zip(request["counts"], FIELDS))


def summarize(rows, sessions, policy):
    total = sum(weighted(r, policy) for r in rows)
    result = {}
    for role in sorted({s["role"] for s in sessions.values()}):
        selected = [r for r in rows if sessions[r["session"]]["role"] == role]
        amount = sum(weighted(r, policy) for r in selected)
        result[role] = dict(requests=len(selected), sessions=len({r["session"] for r in selected}),
                            weighted=amount, share=amount / total if total else 0,
                            subagent_share=sum(weighted(r, policy) for r in selected if r["subagent"]) / amount if amount else 0)
        result[role].update(zip(("input", "output", "cache_write", "cache_read"),
                               (sum(r["counts"][i] for r in selected) for i in range(4))))
    return result


def arms_from(args):
    arms = {}
    for specification in args.arm:
        name, files = specification.split("=", 1)
        paths = files.split(",")
        if name in arms or len(paths) > 2:
            raise ValueError("invalid arm")
        arms[name] = dict(keys={canonical(k) for k in lines(paths[0])},
                          sessions=lines(paths[1]) if len(paths) == 2 else set())
    all_keys, all_sessions = set(), set()
    for arm in arms.values():
        if all_keys & arm["keys"] or all_sessions & arm["sessions"]:
            raise ValueError("overlapping arms")
        all_keys |= arm["keys"]
        all_sessions |= arm["sessions"]
    return arms


def key_report(args, arms, rows, sessions, policy):
    # Without a saved baseline use this window's class medians; --weights pins
    # the comparison to an independent baseline across multiple reports.
    fixed = load_json(args.weights) if args.weights else {}
    if args.weights and not fixed:
        raise ValueError("empty input")
    if not args.weights:
        for role in {s["role"] for s in sessions.values()}:
            values = [r["counts"][3] * policy["weights"][FIELDS[3]] for r in rows
                      if sessions[r["session"]]["role"] == role]
            if values:
                fixed[role] = statistics.median(values)
    accepted = {canonical(k) for k in lines(args.landed)} if args.landed else set()
    if args.snapshot:
        items = load_json(args.snapshot)["items"]
        items = items.values() if isinstance(items, dict) else items
        items = list(items)
        if not items:
            raise ValueError("empty input")
        accepted |= {canonical(i["key"]) for i in items if i["state"] == "done"}
    results = {name: dict(actual=0, fixed=0, turns=0, keys_named=set()) for name in arms}
    for row in rows:
        owner = next((name for name, arm in arms.items()
                      if sessions[row["session"]]["id"] in arm["sessions"]), None)
        hits = {name: row["keys"] & arm["keys"] for name, arm in arms.items()}
        denominator = sum(map(len, hits.values()))
        for name in arms:
            fraction = float(name == owner) if owner else (len(hits[name]) / denominator if denominator else 0)
            if not fraction:
                continue
            actual = weighted(row, policy)
            role = sessions[row["session"]]["role"]
            if not owner and role not in fixed:
                raise ValueError("missing fixed role weight")
            replacement = actual if owner else (actual - row["counts"][3] * policy["weights"][FIELDS[3]] + fixed[role])
            target = results[name]
            target["actual"] += fraction * actual
            target["fixed"] += fraction * replacement
            target["turns"] += fraction
            target["keys_named"] |= hits[name]
    for name, result in results.items():
        count = len(arms[name]["keys"] & accepted)
        if not count or not result["turns"]:
            results[name] = dict(status="no result", reason="zero accepted or attributed turns")
            continue
        result["status"] = "ok"
        result.update(accepted=count, tokens_per_accepted_actual=result["actual"] / count,
                      tokens_per_accepted_fixed=result["fixed"] / count)
        result["keys_named"] = sorted(result["keys_named"])
    if all(result["status"] == "no result" for result in results.values()):
        raise ValueError("zero accepted or attributed turns")
    return results


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--projects", type=Path, default=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--projects", type=Path, default=Path.home() / ".claude" / "projects")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("pace", "daily", "roles", "weights", "keys"):
        cmd = sub.add_parser(name, parents=[common])
        if name == "pace":
            cmd.add_argument("--day", default=datetime.now(timezone.utc).date().isoformat())
        elif name == "weights":
            cmd.add_argument("--baseline-since", required=True)
            cmd.add_argument("--baseline-until", required=True)
            cmd.add_argument("--out", required=True)
        else:
            cmd.add_argument("--since")
            cmd.add_argument("--until")
        if name == "keys":
            cmd.add_argument("--arm", action="append", required=True)
            for flag in ("weights", "snapshot", "landed"):
                cmd.add_argument("--" + flag)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    stats = dict(malformed_lines=0)
    try:
        policy = load_json(Path(__file__).with_name("claude_usage_policy.json"))
        arms = arms_from(args) if args.command == "keys" else {}
        keys = set().union(*(a["keys"] for a in arms.values()))
        patterns = {k: re.compile(r"(?<![\w:.-])(?:[\w.-]+:)?" + re.escape(k) + r"(?![\w:-])") for k in keys}
        rows, sessions = read_projects(args.projects, patterns, stats)
        if not rows:
            raise ValueError("no transcripts")
        if args.command == "pace":
            start = instant(args.day)
            end = start + timedelta(days=1)
        else:
            since = args.baseline_since if args.command == "weights" else args.since
            until = args.baseline_until if args.command == "weights" else args.until
            start = instant(since) if since else datetime.min.replace(tzinfo=timezone.utc)
            end = instant(until) if until else datetime.max.replace(tzinfo=timezone.utc)
        if end <= start:
            raise ValueError("invalid window")
        rows = [r for r in rows if start <= r["time"] < end]
        if args.command == "pace":
            total = sum(weighted(r, policy) for r in rows)
            budget = policy["weekly_budget_weighted"] / 7
            if args.json:
                result = dict(day=start.date().isoformat(), weighted=total, budget_per_day=budget, over_pace=total > budget)
            elif total > budget:
                print(f"over pace: {start.date()} weighted={total:g} budget_per_day={budget:g}; malformed_lines={stats['malformed_lines']}; {UNSEEN}")
                return 0
            else:
                if stats["malformed_lines"]:
                    print(f"malformed_lines={stats['malformed_lines']}; {UNSEEN}", file=sys.stderr)
                return 0
        elif not rows:
            raise ValueError("empty window")
        elif args.command == "roles":
            result = summarize(rows, sessions, policy)
            for role, report in result.items():
                report["starts"] = sum(s["role"] == role and s["start"] is not None and start <= s["start"] < end for s in sessions.values())
        elif args.command == "daily":
            result = {}
            for day in sorted({r["time"].date().isoformat() for r in rows}):
                selected = [r for r in rows if r["time"].date().isoformat() == day]
                models = defaultdict(float)
                for r in selected:
                    models[r["model"]] += weighted(r, policy)
                result[day] = dict(weighted=sum(models.values()), per_model=dict(models),
                                   per_role={k: v["weighted"] for k, v in summarize(selected, sessions, policy).items()},
                                   requests=len(selected), sessions=len({r["session"] for r in selected}))
        elif args.command == "weights":
            grouped = defaultdict(list)
            for row in rows:
                grouped[sessions[row["session"]]["role"]].append(row["counts"][3] * policy["weights"][FIELDS[3]])
            result = {role: statistics.median(values) for role, values in grouped.items()}
            Path(args.out).write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
        else:
            result = key_report(args, arms, rows, sessions, policy)
        envelope = dict(result=result, **stats, visibility=UNSEEN)
        if args.json:
            print(json.dumps(envelope, sort_keys=True))
        else:
            print(json.dumps(result, sort_keys=True))
            print(f"malformed_lines={stats['malformed_lines']}; {UNSEEN}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        reason = "unreadable, empty or invalid input; no usable attribution"
        if args.json:
            print(json.dumps(dict(status="no result", reason=reason, **stats, visibility=UNSEEN)))
        else:
            print(f"no result: {reason}; malformed_lines={stats['malformed_lines']}; {UNSEEN}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
