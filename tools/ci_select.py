"""CI affected-suite selector and gate verdict."""

from __future__ import annotations

import argparse
from fnmatch import fnmatchcase
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

#: Jobs that run on every change: never selected, always required by the gate.
ALWAYS: tuple[str, ...] = ("scrub", "file-size", "new-code-lang", "changelog-docs")

SUITES: tuple[str, ...] = (
    "pytest",
    "pytest-postgres",
    "rust",
    "rust-worker",
    "rust-spike",
    "browser",
)

KNOWN_PREFIXES: tuple[str, ...] = (
    "src/",
    "tests/",
    "tools/",
    "vendor/",
    "rust/",
)

# The standalone Temporal worker job also runs the probe admission guard tests.
RUST_WORKER_INPUTS: tuple[str, ...] = (
    "tools/probe_admission_policy_guard.py",
    "rust/test_support/python.rs",
)

# D-5294: Rust tests also compile/read repository inputs outside rust/.
# Keep these before the docs-only shortcut; tests/test_ci_select_rust_inputs.py
# scans Rust sources and rejects repository reads missing from this list.
RUST_INPUTS: tuple[str, ...] = (
    "pyproject.toml",  # pg-mainff verifies the keeper console-entry inventory.
    "src/plangraph/catalog_data.py",
    "src/plangraph/routing_errors.py",
    "deploy/feeder/**",
    "deploy/ci-watch/**",  # rust/plangraph-cli embeds the job matrix (land::main_gh).
    "deploy/systemd/disk-hygiene.env.example",
    "deploy/systemd/keeper-backup.env.example",
    "deploy/systemd/pg-ci-watch.service",  # a ci_watch test pins its flags.
    "docs/ci-watch.md",  # the same test pins the documented command.
    "deploy/systemd/pg-disk-hygiene.service",
    "deploy/systemd/pg-feeder.service",
    "deploy/systemd/pg-keeper-backup.service",
    "deploy/systemd/pg-keeper-backup.timer",
    "deploy/systemd/pg-throughput.service",
    "deploy/systemd/pg-worktree-sweep.service",
    "deploy/systemd/pg-wsl-cache-guard.service",
    "deploy/systemd/pg-wsl-cache-guard.timer",
    "deploy/systemd/plangraph-state-shadow@.service",
    "deploy/liveness/router.toml",
    "tools/liveness_watchdog.py",
    "src/plangraph/keeper/errors.py",
    "src/plangraph/planner/routing.py",
    "src/plangraph/planner/benchmarks.py",
    "src/plangraph/planner/route_policy.py",
    "src/plangraph/routing_support/catalog_db.py",
    "src/plangraph/routing_support/clients.py",
    "src/plangraph/planner/measured_down.py",
    # D-6054: native/agy quota isolation regressions exercise these boundaries.
    "src/plangraph/planner/provider_stop.py",
    "src/plangraph/planner/measured_down.py",
    "src/plangraph/routing_support/liveness.py",
    # D-6054: rendered OpenCode catalog integration-test inputs.
    "src/plangraph/planner/legs.py",
    "src/plangraph/routing_support/__init__.py",
    "src/plangraph/routing_support/catalog_schema.sql",
    "configuration/benchmark_data_pending.json",
    "src/plangraph/routing_support/opencode_overlay.py",
    "src/plangraph/routing_support/opencode_render.py",
    "src/plangraph/routing_support/resolver.py",
    "configuration/opencode/providers.json",
    # rust/pg-spawnd/src/round_progress_tests.rs reads the max_rounds setting (one stop rule).
    "src/plangraph/settings.py",
    ".github/workflows/**",
    # rust/pg-spawnd/tests/ci_step_timeout.rs reads the bounded-step run script.
    ".github/actions/bounded-step/action.yml",
    "rust-toolchain.toml",
    ".docs-map",
    ".gitignore",
    "docs/schemas/**",
    "docs/flow-watchdog.md",
    "docs/placement-rules.md",
    "docs/rust-hooks.md",
    "docs/spawner-reliability.md",
    "docs/workspace-resolver.md",
    "skills/batch-writer/SKILL.md",  # The Claude writer launcher embeds the skill.
    "skills/review/SKILL.md",  # Native writer review composition fixture.
    "tools/hooks/install_hooks.sh",  # The lesson Stop hook tests its installer.
    "tools/seat_record.py",  # Native review seat binding fixture.
    "tools/merge_batch_build.py",  # Landing journal and merge-policy fixtures.
    "tools/pgtest.py",  # Process and database helpers loaded by the preflight fixtures.
    "skills/router-core/SKILL.md",
    "changelog.d/FLEET-WINDOW-CATALOG.md",
    "changelog.d/PLACEMENT-RULES-AUDIT.md",
    "changelog.d/SPAWNER-RL12-B1.md",
    "deploy/systemd/plangraph-agents-view.service",
    "deploy/systemd/plangraph-agents-view.timer",
    "docs/setup.md",
    "setup/manifest.toml",
    "setup/placement-rules.schema.toml",
    "src/plangraph/web/agents_schema.json",
    "src/plangraph/config/prompt_transport_v1.json",
    # rust/plangraph-cli/src/runner/config.rs tests parse the example runner config.
    "docs/operations/ci-runners.example.toml",
    "src/plangraph/spawner/**",
    "src/plangraph/keeper/__init__.py",
    "src/plangraph/keeper/auth.py",
    "src/plangraph/keeper/context_sources.py",
    "src/plangraph/keeper/db.py",
    "src/plangraph/keeper/evidence.py",
    "src/plangraph/keeper/evidence_decisions.py",
    "src/plangraph/keeper/evidence_types.py",
    "src/plangraph/keeper/store.py",
    "src/plangraph/keeper/evidence_spawner.py",
    "src/plangraph/routing_support/leg_state.py",
    "tests/fixtures/doctor_sources.json",
    "tests/fixtures/files_read.json",
    "src/plangraph/config/role_headers.json",
    "tests/fixtures/keeper_mcp_replies.json",
    "tests/fixtures/keeper_mcp_tools.json",
    "tests/fixtures/redact_parity.json",
    # rust/pg-spawnd/src/round_replan_ports_tests.rs asserts the golden slice definitions.
    "tests/fixtures/replan_slice_definition.json",
    "tests/fixtures/writer_docs_map.json",
    "tests/hooks/parity_payloads.json",
    "tools/changelog_fold.py",
    "tools/check_changelog_docs.py",
    "tools/check_file_size.py",
    "tools/check_new_code_lang.py",
    "tools/ci_select.py",
    "tools/claude_usage_policy.json",
    "tools/ci_commit_gate.py",
    "tools/check_push_gate.py",
    "tools/merge_batch_util.py",
    "tools/gate_wiring_check.py",
    "tools/split/**",
    "tools/hooks/pre-push",
    "tools/merge_batch.py",
    "tools/merge_batch_land.py",
    "tools/dell_ci_jobs.py",
    "tools/new_code_allowlist.txt",
    *RUST_WORKER_INPUTS,
    "tools/seat_verdict.py",
    "tools/preflight.py",
    "tools/preflight_private_scrub.py",
)


def _normalize_path(p: str) -> str:
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _is_ci_deps(p: str) -> bool:
    return (
        p.startswith(".github/")
        or p == "requirements-ci.txt"
        or p == "tools/ci_select.py"
    )


def _is_docs_class(p: str) -> bool:
    basename = p.split("/")[-1]
    return (
        p.endswith(".md")
        or p.startswith("docs/")
        or basename.startswith("CHANGELOG")
        or basename == ".docs-map"
    )


def _is_known(p: str) -> bool:
    if _is_docs_class(p):
        return True
    return any(p.startswith(prefix) for prefix in KNOWN_PREFIXES)


def select(paths: list[str]) -> dict[str, bool]:
    norm_paths: list[str] = []
    for raw in paths:
        p = _normalize_path(raw)
        if not p.strip():
            continue
        norm_paths.append(p)

    # 1. No paths at all (empty diff) -> full run (fail closed)
    if not norm_paths:
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    if all(p.startswith(".github/workflows/") for p in norm_paths):
        # Rust regression tests also read the workflows as repository inputs.
        return {suite: suite in {"pytest", "rust"} for suite in SUITES} | {"full": False}

    # 2. Any other CI/deps change -> full run
    if any(_is_ci_deps(p) for p in norm_paths):
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    # 3. Any UNKNOWN path -> full run
    if any(not _is_known(p) for p in norm_paths):
        res = {s: True for s in SUITES}
        res["full"] = True
        return res

    # 5. Otherwise (no full trigger) select by path (ALWAYS jobs need no selection)
    res = {s: False for s in SUITES}
    res["full"] = False

    for p in norm_paths:
        if p.startswith(("src/plangraph/web/", "tests/browser/", "vendor/")) or p in ("tests/conftest.py", "pyproject.toml", "src/plangraph/render.py"):
            res["browser"] = True
        if any(fnmatchcase(p, pattern) for pattern in RUST_INPUTS):
            res["rust"] = True
        if p in RUST_WORKER_INPUTS:
            res["rust-worker"] = True
        if p.startswith("skills/") and p.endswith(".md"):
            # skill pages are guarded by tests/test_skill_*.py
            res["pytest"] = True
            continue
        if _is_docs_class(p):
            continue
        if any(p.startswith(prefix) for prefix in ("src/", "tests/", "tools/", "vendor/")):
            res["pytest"] = True
            res["pytest-postgres"] = True
        elif p.startswith("rust/"):
            res["pytest"] = True
            res["pytest-postgres"] = True
            if p.startswith("rust/spikes/"):
                res["rust-spike"] = True
            if (p.startswith(("rust/worker/", "rust/worker-core/", "rust/pg-spawnd/"))
                    or fnmatchcase(p, "rust/Cargo.*")):
                res["rust-worker"] = True
            if (
                (not p.startswith("rust/spikes/") and not p.startswith("rust/worker/"))
                or p.startswith("rust/worker-core/")
            ):
                res["rust"] = True

    return res


#: Label a router GO row puts on a pull request head to name it the merge candidate (D-6392 run-once order).
FULL_LABEL = "ci-full"
BATCH_PREFIX = "batch/"
#: A landing train's head branch: always the full suite, once, directly before the merge into main.
TRAIN_PREFIX = "land/train-"
#: Label of a part pull request (a batch waiting for a train): it runs the affected selection even on a batch/* head.
PART_LABEL = "land-part"


def full_run_required(event: str, base_ref: str = "", head_ref: str = "", labels=()) -> bool:
    """The full suite runs once per merge candidate; every part and lane pull request gets the affected selection.

    A pull_request event gets the full suite only for a train head (TRAIN_PREFIX), a batch/* head or a head
    carrying FULL_LABEL, and only when it targets main. A part (PART_LABEL) that is not a train head runs the
    affected-only mode, whatever its branch name, draft state or other labels.
    Push, schedule, workflow_dispatch and merge_group events are the record or the candidate itself: full."""
    if event != "pull_request":
        return True
    if base_ref != "main":
        return False
    names = set(labels or ())
    if head_ref.startswith(TRAIN_PREFIX):
        return True
    if PART_LABEL in names:   # a part is never a candidate, not even with a stray ci-full label
        return False
    return FULL_LABEL in names or head_ref.startswith(BATCH_PREFIX)


#: Commit-status contexts of the landing mode ``tested-ff`` (LAND-MODE-TESTED-FF). ``land-auth`` is the
#: authorization only the land App writes; ``ci-full`` is the evidence the full run of the train pull request
#: posts for the commit it tested.
LAND_AUTH = "land-auth"
CI_FULL = "ci-full"
_AUTH_EVIDENCE = re.compile(r"attempt \S+ evidence run (\d+) attempt (\d+)")
_FULL_EVIDENCE = re.compile(r"run (\d+) attempt (\d+) tree ([0-9a-f]{40})")


def _latest(statuses, context: str):
    found = [s for s in statuses if isinstance(s, dict) and s.get("context") == context]
    return max(found, key=lambda s: s.get("id") or 0) if found else None


def ci_full_description(run_id, attempt, tested_tree: str, head_tree: str):
    """The description of the ``ci-full`` status of a full run (``run <id> attempt <n> tree <tree>``), or None
    when the tree the run tested is not the tree of the head commit it would vouch for."""
    if not tested_tree or tested_tree != head_tree:
        return None
    return f"run {run_id} attempt {attempt} tree {tested_tree}"


def landing_evidence(statuses, tree: str, creator_id):
    """``"<run id> attempt <n>"`` when the commit is a tested landing, else None (the full suite runs).

    A tested landing has the newest ``land-auth`` status in state success, created by ``creator_id`` (the id of
    the land App's bot user; statuses report their creator, and an unset id never matches), whose description
    names an evidence run and attempt, and the newest ``ci-full`` status in state success whose description
    names that same run and attempt and the tree of this commit (``tree``). Anything else, a newer failed
    ``land-auth`` included, is not tested: a push run then runs everything, as before."""
    if not creator_id or not tree:
        return None
    auth = _latest(statuses, LAND_AUTH)
    if not auth or auth.get("state") != "success" or (auth.get("creator") or {}).get("id") != creator_id:
        return None
    named = _AUTH_EVIDENCE.fullmatch(auth.get("description") or "")
    full = _latest(statuses, CI_FULL)
    seen = _FULL_EVIDENCE.fullmatch((full or {}).get("description") or "")
    if not named or not full or full.get("state") != "success" or not seen:
        return None
    if seen.group(1, 2) != named.group(1, 2) or seen.group(3) != tree:
        return None
    return f"{named.group(1)} attempt {named.group(2)}"


def gate(needs: dict, require_full: bool = False) -> list[str]:
    if not isinstance(needs, dict) or "select" not in needs or not isinstance(needs["select"], dict):
        return ["select: missing"]

    select_job = needs["select"]
    select_result = select_job.get("result")
    if select_result != "success":
        return [f"select: {select_result if select_result is not None else 'missing'}"]

    outputs = select_job.get("outputs")
    if not isinstance(outputs, dict):
        outputs = {}

    problems: list[str] = []
    # A push run of a commit with landing evidence ran only the fast jobs on purpose (landing_evidence).
    if require_full and outputs.get("full") != "true" and outputs.get("tested") != "true":
        problems.append("select: merge candidate ran the affected selection, not the full suite")
    for suite in ALWAYS + SUITES + ("rust-windows",):
        selection = "rust" if suite == "rust-windows" else suite
        selected = outputs.get("full") == "true" or suite in ALWAYS or outputs.get(selection) != "false"
        if not selected:
            continue

        if suite not in needs or not isinstance(needs[suite], dict):
            problems.append(f"{suite}: missing")
        else:
            suite_result = needs[suite].get("result")
            if suite_result != "success":
                problems.append(
                    f"{suite}: {suite_result if suite_result is not None else 'missing'}"
                )

    return problems


# --- part entry: the entry set, the readiness verdict and the per-part cost (LAND-PART-ENTRY-CHECKS) -------------

TOOLS_DIR = Path(__file__).resolve().parent


def _load_tool(name: str):
    """Import a sibling tool lazily: the CI select job runs without the package installed."""
    import importlib.util

    sys.path.insert(0, str(TOOLS_DIR.parent / "src"))
    spec = importlib.util.spec_from_file_location(f"ci_select_{name}", TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry_set(root, base: str, head: str) -> dict:
    """What a part must pass on its head before it enters a train.

    Suites come from select(); Rust crates (with their reverse dependencies) from tools/preflight.py;
    Python tests from the affected selector, whose unknown-input fallback is the full suite (rule 3.9).
    A change that selects Rust CI, also a Python-only one, needs the native Windows job."""
    root = Path(root)
    raw = subprocess.run(["git", "diff", "--name-only", "-z", f"{base}...{head}"], cwd=root,
                         capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    paths = [p for p in raw.split("\0") if p]
    suites = select(paths)
    python_tests = _load_tool("affected_tests").select_tests(root, paths, local=True, base=base, head=head)["tests"]
    fallback = "tests" in python_tests or suites["full"]
    packages: list[str] = []
    rust_tests: list[list[str]] = []
    if any(p.startswith("rust/") or p == "rust-toolchain.toml" for p in paths):
        preflight = _load_tool("preflight")
        try:
            commands = preflight.rust_commands(root, base, head)
        except preflight.SkipFamily:
            commands = []
        for command in commands:
            if command[:2] == ["cargo", "test"]:
                rust_tests.append(command[2:])   # every selected test command, standalone manifests included
                packages += [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-p"]
    return {"paths": paths, "suites": {k: v for k, v in suites.items()}, "python_tests": python_tests,
            "rust_packages": sorted(set(packages)), "rust_tests": rust_tests, "fallback": fallback, "windows": bool(suites["rust"])}


def entry_commands(plan: dict, base: str = "origin/main", head: str = "HEAD", root=None) -> list[list[str]]:
    """The commands that run an entry set on Linux; the Windows run is separate (a plan's `windows`)."""
    commands: list[list[str]] = []
    if plan.get("fallback"):
        commands.append([sys.executable, "-m", "pytest", "tests", "-q"])   # unknown input: the full suite
    elif plan.get("python_tests"):
        commands.append([sys.executable, str(TOOLS_DIR / "affected_tests.py"), "--base", base, "--head", head,
                         *(["--root", str(root)] if root else []), "--local", "--run"])
    tests = plan.get("rust_tests")
    if tests is None and plan.get("rust_packages"):
        tests = [[arg for package in plan["rust_packages"] for arg in ("-p", package)]]
    commands += [["cargo", "test", "--no-fail-fast", *arguments] for arguments in tests or []]
    return commands


def attempt_key(attempt):
    return attempt.get("run_id", 0), attempt["attempt"]


def attempt_problems(attempts: list[dict], flake_dispositions=()) -> list[str]:
    """Readiness (d): the latest completed attempt on the head must not be red; a red attempt followed by a
    green one clears only with the flake disposition of rule 3.46 recorded for the red attempt."""
    done = sorted((a for a in attempts if a.get("conclusion")), key=attempt_key)
    if not done:
        return ["no completed attempt on this head"]
    latest = done[-1]
    if latest["conclusion"] != "success":
        return [f"latest completed attempt {latest['attempt']} is {latest['conclusion']}"]
    flakes = {(0, value) if isinstance(value, int) else tuple(value) for value in flake_dispositions}
    return [f"attempt {a['attempt']} was {a['conclusion']} and attempt {latest['attempt']} green: "
            "no flake disposition recorded"
            for a in done[:-1] if a["conclusion"] != "success" and attempt_key(a) not in flakes]


def ready_problems(facts: dict) -> list[str]:
    """What still stands between a part and READY on its exact head (the run facts are gathered by the caller)."""
    problems = []
    if not facts.get("fast_checks"):
        problems.append("seconds-fast checks did not pass on this head")
    if not facts.get("entry_set"):
        problems.append("entry set did not pass on this head")
    if facts.get("windows_required"):
        if facts.get("windows") is None:
            problems.append("native Windows entry run missing")
        elif not facts["windows"]:
            problems.append("native Windows entry run failed")
    return problems + attempt_problems(facts.get("attempts", []), facts.get("flake_dispositions", []))


def _seconds(start: str, end: str) -> int:
    parse = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))  # noqa: E731
    return int((parse(end) - parse(start)).total_seconds())


def part_cost(jobs: list[dict], pr: int, head: str) -> dict:
    """Queue and run seconds of the CI jobs of one part head, and whether each ran on a warm (self-hosted) runner."""
    rows = [{"name": j["name"], "queue_seconds": _seconds(j["created_at"], j["started_at"]),
             "run_seconds": _seconds(j["started_at"], j["completed_at"]),
             "runner": "warm" if "self-hosted" in j.get("labels", []) else "cold"} for j in jobs]
    first = min(j["created_at"] for j in jobs)
    last = max(j["completed_at"] for j in jobs)
    return {"pr": pr, "head": head, "jobs": rows, "queue_seconds": sum(r["queue_seconds"] for r in rows),
            "run_seconds": sum(r["run_seconds"] for r in rows), "wall_seconds": _seconds(first, last)}


def write_part_cost(path, record: dict) -> None:
    """Append one JSON line to the part-cost telemetry file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CI selector and gate verdict")
    subparsers = parser.add_subparsers(dest="command")

    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--full", action="store_true", help="Force full CI run")
    select_parser.add_argument("--base", type=str, default=None, help="Base commit SHA")
    select_parser.add_argument("--head", type=str, default=None, help="Head commit SHA")
    select_parser.add_argument("--files", nargs="*", default=None, help="Explicit list of files")
    entry_parser = subparsers.add_parser("entry-set", help="the entry set of a part head; --run executes it")
    entry_parser.add_argument("--base", required=True)
    entry_parser.add_argument("--head", required=True)
    entry_parser.add_argument("--root", default=".")
    entry_parser.add_argument("--run", action="store_true")
    cost_parser = subparsers.add_parser("part-cost", help="append the CI cost of a part head to telemetry")
    cost_parser.add_argument("--jobs-json", required=True, help="file with the jobs of the run (GitHub jobs API)")
    cost_parser.add_argument("--pr", type=int, required=True)
    cost_parser.add_argument("--head", required=True)
    cost_parser.add_argument("--telemetry-dir", default=os.environ.get("PLANGRAPH_TELEMETRY_DIR") or
                             str(Path.home() / ".local/share/plangraph/telemetry"))
    gate_parser = subparsers.add_parser("gate")
    for sub in (select_parser, gate_parser):
        sub.add_argument("--event", default=None, help="GitHub event name; picks full or affected selection")
        sub.add_argument("--base-ref", default="", help="Pull request base branch name")
        sub.add_argument("--head-ref", default="", help="Pull request head branch name")
        sub.add_argument("--labels", default="[]", help="JSON list of the pull request label names")

    select_parser.add_argument("--statuses", default=None, help="JSON list of the commit statuses of the pushed commit")
    select_parser.add_argument("--tree", default="", help="tree id of the pushed commit")
    select_parser.add_argument("--land-creator-id", type=int, default=None, help="id of the land App's bot user")
    ci_full_parser = subparsers.add_parser("ci-full")
    ci_full_parser.add_argument("--run-id", required=True)
    ci_full_parser.add_argument("--attempt", required=True)
    ci_full_parser.add_argument("--tested-tree", required=True)
    ci_full_parser.add_argument("--head-tree", required=True)

    gate_parser.add_argument("--needs", type=str, required=True, help="JSON string of needs object")

    args = parser.parse_args(argv)

    if args.command == "ci-full":
        text = ci_full_description(args.run_id, args.attempt, args.tested_tree, args.head_tree)
        if text is None:
            sys.stderr.write("ci-full: the tested tree is not the head tree; no evidence posted\n")
            return 1
        sys.stdout.write(text + "\n")
        return 0

    candidate = False
    if args.command in ("select", "gate") and args.event is not None:
        try:
            labels = json.loads(args.labels) if args.labels else []
            if not isinstance(labels, list):
                raise ValueError
        except ValueError:
            sys.stderr.write("ci_select: --labels must be a JSON list\n")
            return 1
        candidate = full_run_required(args.event, args.base_ref, args.head_ref, labels)

    if args.command == "select":
        if args.full or candidate:
            selection = {s: True for s in SUITES} | {"full": True}
        elif args.base is not None or args.head is not None:
            if args.base is not None and args.head is not None:
                try:
                    proc = subprocess.run(
                        ["git", "diff", "--name-only", "-z", f"{args.base}...{args.head}"],
                        capture_output=True,
                        check=True,
                    )
                    raw_text = proc.stdout.decode("utf-8", errors="replace")
                    paths = [p for p in raw_text.split("\0") if p]
                    selection = select(paths)
                except Exception:
                    selection = {s: True for s in SUITES} | {"full": True}
            else:
                selection = {s: True for s in SUITES} | {"full": True}
        elif args.files is not None:
            selection = select(args.files)
        else:
            selection = {s: True for s in SUITES} | {"full": True}

        evidence = None
        if args.event == "push" and args.statuses:
            try:
                with open(args.statuses, encoding="utf-8") as handle:
                    evidence = landing_evidence(json.load(handle), args.tree, args.land_creator_id)
            except (OSError, ValueError):
                evidence = None  # an unreadable record is no evidence: the full suite runs
        if evidence:
            selection = {s: False for s in SUITES} | {"full": False}
        lines = [f"{s}={'true' if selection[s] else 'false'}" for s in SUITES]
        lines.append(f"full={'true' if selection['full'] else 'false'}")
        if evidence:
            lines += ["tested=true", f"evidence={evidence}"]
        output_str = "\n".join(lines) + "\n"

        sys.stdout.write(output_str)

        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(output_str)
        return 0

    if args.command == "entry-set":
        root = Path(args.root).resolve()
        plan = entry_set(root, args.base, args.head)
        print(json.dumps(plan, sort_keys=True), flush=True)
        # Import the part's source before any installed or inherited checkout.
        python_path = [str(root / "src"), os.environ.get("PYTHONPATH")]
        python_env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, python_path)))
        failed = 0
        for command in entry_commands(plan, args.base, args.head, root) if args.run else []:
            is_cargo = command[0] == "cargo"
            cwd = root / "rust" if is_cargo else root
            if subprocess.run(command, cwd=cwd, env=None if is_cargo else python_env).returncode:
                print(f"entry-set: FAIL: {' '.join(command[:4])}", file=sys.stderr)
                failed = 1
        return failed

    if args.command == "part-cost":
        jobs = json.loads(Path(args.jobs_json).read_text(encoding="utf-8"))
        record = part_cost(jobs["jobs"] if isinstance(jobs, dict) else jobs, args.pr, args.head)
        write_part_cost(Path(args.telemetry_dir) / "part-cost.jsonl", record)
        print(json.dumps(record, sort_keys=True))
        return 0

    if args.command == "gate":
        try:
            needs_obj = json.loads(args.needs)
        except Exception:
            sys.stderr.write("ci-gate: bad needs json\n")
            return 1

        problems = gate(needs_obj, require_full=candidate)
        if problems:
            for prob in problems:
                sys.stderr.write(f"{prob}\n")
            return 1

        sys.stdout.write("ci-gate: ok\n")
        return 0

    parser.print_help(sys.stderr)
    return 1


# Train 1 took 414 s when idle and 655 s at load ~27 on the same host.
RUST_TIMEOUT = 600
RUST_PER_BINARY_TIMEOUT = 15
MAX_RUST_TIMEOUT = 3600


def rust_bound(binaries, load, cores, *, floor=RUST_TIMEOUT):
    base = max(floor, binaries * RUST_PER_BINARY_TIMEOUT)
    saturation = min(max(load, 0.0), cores) / max(cores, 1)
    return min(MAX_RUST_TIMEOUT, base * (1 + saturation))


def host_load():
    """(1-minute load average, cores); load 0 where the platform has none."""
    try:
        load = os.getloadavg()[0]
    except (OSError, AttributeError):
        load = 0.0
    return load, os.cpu_count() or 2


def planned_test_binaries(commands, root):
    """Test binaries the cargo test commands build: one lib/bin target plus every tests/*.rs per crate."""
    root = Path(root)
    if root.name == "rust":   # cargo commands run inside rust/
        root = root.parent
    owners = {}
    for manifest in (root / "rust").rglob("Cargo.toml"):
        if "target" in manifest.parts:
            continue
        try:
            name = tomllib.loads(manifest.read_text()).get("package", {}).get("name")
        except (OSError, ValueError):
            continue
        if name:
            owners[name] = manifest.parent
        owners[str(manifest.resolve())] = manifest.parent
    total = 0
    for command in commands:
        if command[:2] != ["cargo", "test"]:
            continue
        crates = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-p"]
        crates += [str(Path(command[i + 1]).resolve()) for i, arg in enumerate(command[:-1])
                   if arg == "--manifest-path"]
        for crate in crates or list({str(d): d for n, d in owners.items() if n and not n.endswith("Cargo.toml")}):
            directory = owners.get(crate, Path(crate) if Path(crate).is_dir() else None)
            if directory is not None:
                total += 1 + len(list((directory / "tests").glob("*.rs")))
    return total


def running_test_binary(partial):
    """The test binary named by the last cargo "Running" line of the output so far, else None."""
    if isinstance(partial, bytes):
        partial = partial.decode("utf-8", "replace")
    found = re.findall(r"^\s*Running (\S+(?: \S+)?) \(", partial or "", re.M)
    return found[-1] if found else None


if __name__ == "__main__":
    raise SystemExit(main())
