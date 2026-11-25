"""Keeper evidence verifiers (see docs/KEEPER.md, "Evidence verifiers").

Verifiers accept (item, ref, ctx) and use injectable I/O adapters. The guarded
entry points turn exceptions into failed results containing only the exception
class. Commit+CI binds a full SHA to the target ref through its introducing PR,
the item's Plan-Item trailer and its ancestry floor (when required), then
checks CI for that exact SHA.
"""
from __future__ import annotations

import functools
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

from .evidence_ci import CIAdapter
from .evidence_decisions import DecisionsAdapter
from .evidence_floor import check_floor
from .evidence_pr import validate_merge
from .evidence_forgejo import ForgejoStatusAdapter
from .evidence_git import GitAdapter, LegacyCommitVerifier
from .evidence_repo import repo_key
from .evidence_runs import RunRecordAdapter
from .evidence_spawner import SpawnerRecordAdapter
from .evidence_semaphore import SemaphoreAdapter, SemaphoreError, positive_int
from .item_fixes import ErrorRecordVerifier
from .evidence_types import (
    DEFAULT_SKEW_S,
    Context,
    Result,
    as_epoch,
    oneshot_timer,
    proven_pairs,
    valid_commit_ref,
)
__all__ = [
    "CommitCiVerifier",
    "RunRecordVerifier",
    "SemaphoreTaskVerifier",
    "DecisionVerifier",
    "DeployVerifier",
    "deploy_targets",
    "deploy_hosts",
    "deploy_missing",
    "VERIFIERS",
    "default_context",
    "verify",
    "Result",
    "Context",
]


# small helpers

def _trailer(trailers: Any, name: str) -> str | None:
    """Case-insensitive trailer lookup (git trailer keys are case-insensitive)."""
    if not isinstance(trailers, dict):
        return None
    target = name.lower()
    for key, value in trailers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _fail(detail: str, ref: dict) -> Result:
    return Result(False, detail, ref)


def _ref(ref: Any) -> dict:
    return dict(ref) if isinstance(ref, dict) else {}


def _guarded(fn):
    """Backstop: any escaping exception becomes a sanitised failure Result."""

    @functools.wraps(fn)
    def wrapper(self, item, ref, ctx):
        try:
            return fn(self, item, ref, ctx)
        except Exception as exc:  # noqa: BLE001 - a verifier must never raise
            try:
                safe_ref = _ref(ref)
            except Exception:  # noqa: BLE001
                safe_ref = {}
            return Result(False, type(exc).__name__, safe_ref)

    return wrapper


# commit+ci

def _ci_conclusion(runs: Any, commit: str) -> str | None:
    """Newest-run-per-group CI verdict: None means ok, else the fail detail. Plain groups are (suite, app, name); a workflow_dispatch run forms a ("wf", workflow_id, app, name) group across suites that a matching PR run joins. Inside a wf group the best run is the NEWEST COMPLETED run whose conclusion (stripped, lowercased) is not skipped/neutral, so a job-level `if:` skipping the newest run cannot mask an older real verdict; only when every completed run there is skipped/neutral does the newest of them win (so 'CI only skipped' still applies). A 'workflow' dict lacking a positive workflow_id or run_id counts as no 'workflow' at all. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""

    def _id_int(value: Any) -> int:
        # Same coercion as check-run ids: bool is not an id, digit strings rank as ints.
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return 0

    def _hashable(value: Any) -> Any:
        # gh hands us ints; an unhashable id from a fake is then simply no key.
        try:
            hash(value)
        except TypeError:
            return None
        return value

    # Dispatch runs group with matching PR runs.
    parsed: list[dict] = []
    dispatch_keys: set = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name", "?"))
        suite: Any = None
        cs = run.get("check_suite")
        if isinstance(cs, dict) and "id" in cs:
            suite = _hashable(cs["id"])
        app: Any = None
        ap = run.get("app")
        if isinstance(ap, dict) and "slug" in ap:
            app = _hashable(ap["slug"])
        rid_int = _id_int(run.get("id"))
        started = run.get("started_at")
        started_str = str(started) if started is not None else ""
        wf = run.get("workflow")
        wf_id = _id_int(wf.get("workflow_id")) if isinstance(wf, dict) else 0
        wf_rid = _id_int(wf.get("run_id")) if isinstance(wf, dict) else 0
        # Require both IDs so invalid workflow data cannot group unrelated runs.
        in_wf = wf_id > 0 and wf_rid > 0
        event: str | None = None
        if in_wf:
            raw_event = wf.get("event")
            event = str(raw_event) if raw_event is not None else None
            if event == "workflow_dispatch":
                dispatch_keys.add((wf_id, app, name))
        else:
            wf_id = 0
            wf_rid = 0
        parsed.append({"name": name, "suite": suite, "app": app, "run": run,
                       "event": event, "wf_id": wf_id, "in_wf": in_wf,
                       "key": (rid_int, started_str), "wf_rid": wf_rid})

    # Rank workflow run IDs first because check-run IDs can go backwards on rerun.
    groups: dict[Any, dict] = {}
    for rec in parsed:
        is_wf = rec["event"] == "workflow_dispatch"
        if not is_wf and rec["in_wf"]:
            # Any other event (pull_request, ...) keeps its suite key unless a
            # dispatch exists for the same workflow, app and check name.
            is_wf = (rec["wf_id"], rec["app"], rec["name"]) in dispatch_keys
        if is_wf:
            gkey: Any = ("wf", rec["wf_id"], rec["app"], rec["name"])
            rank: Any = (rec["wf_rid"], rec["key"][0], rec["key"][1])
            sort: Any = rec["wf_id"]
            suffix: Any = f"workflow {rec['wf_id']}"
        else:
            gkey = (rec["suite"], rec["app"], rec["name"])
            rank = rec["key"]
            sort = rec["suite"]
            suffix = f"suite {rec['suite']}" if rec["suite"] is not None else None
        g = groups.get(gkey)
        if g is None:
            g = {"name": rec["name"], "app": rec["app"], "wf": is_wf,
                 "sort": sort, "suffix": suffix, "runs": []}
            groups[gkey] = g
        g["runs"].append((rank, rec["run"]))
    if not groups:
        return f"no CI check-runs found for {commit}"

    def _completed(run: dict) -> bool:
        # Non-empty conclusion and status absent-or-completed (legacy fakes).
        conc_raw = run.get("conclusion")
        if conc_raw is None or str(conc_raw).strip() == "":
            return False
        return not ("status" in run and str(run.get("status")).strip().lower() != "completed")

    def _norm_conc(run: dict) -> str:
        # Stripped, lowercased conclusion ('' when absent/blank).
        conc_raw = run.get("conclusion")
        return "" if conc_raw is None else str(conc_raw).strip().lower()

    pending_groups: list[tuple[str, Any, Any]] = []
    failure_groups: list[tuple[str, Any, Any, str]] = []
    seen: list[str] = []
    for gkey in sorted(groups, key=lambda k: (groups[k]["name"], str(groups[k]["sort"]), str(groups[k]["app"]))):
        g = groups[gkey]
        if g["wf"]:
            # A skipped rerun cannot mask an older verdict. A newer incomplete
            # run keeps the group pending.
            best: tuple[Any, dict] | None = None
            newest_completed: tuple[Any, dict] | None = None
            for rank, run in g["runs"]:
                if not _completed(run):
                    continue
                if newest_completed is None or rank > newest_completed[0]:
                    newest_completed = (rank, run)
                if _norm_conc(run) in ("skipped", "neutral"):
                    continue
                if best is None or rank > best[0]:
                    best = (rank, run)
            if best is None:
                best = newest_completed
            if best is None or any(
                rank > best[0] and not _completed(run) for rank, run in g["runs"]
            ):
                pending_groups.append((g["name"], g["suffix"], g["app"]))
                continue
            run = best[1]
        else:
            # Plain groups keep today's rule: the newest run alone is evaluated.
            best = None
            for rank, run in g["runs"]:
                if best is None or rank > best[0]:
                    best = (rank, run)
            run = best[1]
        conc_raw = run.get("conclusion")
        if conc_raw is None or (
            isinstance(conc_raw, str) and conc_raw.strip() == ""
        ):
            pending_groups.append((g["name"], g["suffix"], g["app"]))
            continue
        conc = str(conc_raw).strip()
        if conc == "":
            pending_groups.append((g["name"], g["suffix"], g["app"]))
            continue
        if "status" in run and str(run.get("status")).strip().lower() != "completed":
            pending_groups.append((g["name"], g["suffix"], g["app"]))
            continue
        norm = conc.lower()
        seen.append(norm)
        if norm in ("success", "skipped", "neutral"):
            continue
        failure_groups.append((g["name"], g["suffix"], g["app"], str(conc_raw)))
    if failure_groups:
        from collections import Counter

        counts = Counter(n for n, _, _, _ in failure_groups)

        def _fail_disp(n: str, s: Any, a: Any) -> str:
            if counts[n] <= 1:
                return n
            if s is not None:
                return f"{n} ({s})"
            if a is not None:
                return f"{n} ({a})"
            return n

        inner = ", ".join(
            f"{_fail_disp(n, s, a)}={c}" for n, s, a, c in failure_groups
        )
        return f"CI is not green for {commit} ({inner})"
    if pending_groups:
        from collections import Counter

        counts = Counter(n for n, _, _ in pending_groups)

        def _pend_disp(n: str, s: Any, a: Any) -> str:
            if counts[n] <= 1:
                return n
            if s is not None:
                return f"{n} ({s})"
            if a is not None:
                return f"{n} ({a})"
            return n

        displays = [_pend_disp(n, s, a) for n, s, a in pending_groups]
        return f"CI pending for {commit} ({', '.join(displays)}), retry later"
    if seen and all(c == "skipped" for c in seen):
        return f"CI only skipped for {commit}"
    return None


def _plan_items(trailers: Any) -> list:
    """The ``Plan-Item`` values of one trailer mapping (a key may repeat as a list)."""
    return [value for key, raw in (trailers.items() if isinstance(trailers, dict) else [])
            if str(key).lower() == "plan-item"
            for value in (raw if isinstance(raw, list) else [raw])]


class CommitCiVerifier:
    """A commit on the target ref merged via PR for this item, descending from ancestry floor, CI green."""

    kind = "commit+ci"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        commit = ref.get("commit")
        if not isinstance(commit, str) or not commit:
            return _fail("commit+ci requires a 'commit' sha", ref)
        if not valid_commit_ref(commit):
            return _fail(
                "commit+ci requires a full lowercase commit sha (40 or 64 hex)", ref
            )

        project = repo_key(item or {}, ctx, commit)
        try:
            repo = ctx.git.repo_path(project) if ctx.git is not None else None
        except Exception:  # noqa: BLE001 - adapter failure is a verification failure
            repo = None
        if not repo:
            return _fail(f"no repo registered for project {project!r}", ref)

        try:
            if not ctx.git.commit_exists(repo, commit):
                return _fail(f"commit {commit} does not exist", ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error checking the commit ({type(exc).__name__})", ref)

        target_ref = "main"
        try:
            # ``repo_ref`` is required of every adapter (no invented "main").
            target_ref = ctx.git.repo_ref(project)
            if hasattr(ctx.git, "is_ancestor"):
                is_anc = ctx.git.is_ancestor(repo, commit, target_ref)
            else:
                is_anc = ctx.git.is_ancestor_of_main(repo, commit)
            if not is_anc:
                return _fail(f"commit {commit} is not an ancestor of {target_ref}", ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error checking {target_ref} ancestry ({type(exc).__name__})", ref)

        try:
            trailers = ctx.git.trailers(repo, commit)
        except Exception:  # noqa: BLE001
            trailers = {}
        wanted = [value for value in (item.get("key"), item.get("id")) if value]
        values = _plan_items(trailers)
        if not any(value in wanted for value in values):
            # A merge made without trailers (the hosted UI merge) stands for the
            # commits of its branch range: any of them naming the item binds it.
            try:
                values = values + list(ctx.git.merge_plan_items(repo, commit))
            except Exception:  # noqa: BLE001 - an unreadable range never binds
                pass
        if not any(value in wanted for value in values):
            label = item.get("key") or item.get("id")
            return _fail(
                f"commit {commit} does not carry trailer 'Plan-Item: {label}'", ref
            )

        fn = getattr(ctx.git, "merge_commit_of", None)
        if fn is None:
            return _fail("git adapter cannot prove a merged PR; failing closed", ref)
        try:
            merge_info = fn(repo, commit, target_ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error checking the merged PR ({type(exc).__name__})", ref)
        if (
            not isinstance(merge_info, tuple)
            or len(merge_info) != 2
            or not isinstance(merge_info[0], str)
            or not isinstance(merge_info[1], str)
        ):
            return _fail(
                f"commit {commit} is not reachable from {target_ref} through a merged PR (direct push or unmerged)",
                ref,
            )
        refusal = validate_merge(ctx, project, merge_info, target_ref, ref)
        if refusal is not None:
            return refusal

        # Use the ancestry floor of the resolved repository.
        refusal = check_floor(ctx, repo, commit, ref, project, item)
        if refusal is not None:
            return refusal

        try:
            ci_mode = ctx.ci.mode(project) if ctx.ci is not None else None
        except Exception:  # noqa: BLE001
            ci_mode = None
        if ci_mode is None:
            return _fail(f"no CI configured for project {project!r}", ref)
        if ci_mode == "none":
            return Result(True, f"commit {commit} on main with Plan-Item (ci: none)", ref)

        try:
            runs = ctx.ci.check_runs(ci_mode, commit)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"CI lookup failed ({type(exc).__name__})", ref)
        if not runs:
            return _fail(f"no CI check-runs found for {commit}", ref)
        if any(str(run.get("head_sha", "")) != commit for run in runs):
            return _fail(f"CI check-runs are for another sha than {commit}", ref)
        detail = _ci_conclusion(runs, commit)
        if detail is not None:
            return _fail(detail, ref)
        return Result(True, f"commit {commit} on main with Plan-Item and green CI", ref)


class ClaudeSeatVerifier:
    """Bounded transport to the keeper-installed Rust transcript verifier."""

    kind = "claude-seat"

    @_guarded
    def verify(self, item, ref, ctx):
        import json
        import os
        import subprocess
        from pathlib import Path
        ref = _ref(ref)
        binary = os.environ.get("PG_CLAUDE_SEAT_BIN", "/usr/local/bin/pg-seat-evidence")
        if not os.path.isabs(binary):
            return _fail("claude_seat_helper_unavailable", ref)
        request = {k: ref.get(k) for k in ("transcript", "review_file", "head")}
        request.update(root=ctx.claude_projects_dir or str(Path.home() / ".claude/projects"),
                       writer_families=item.get("_seat_writer_families", []))
        try:
            proc = subprocess.run([binary], input=json.dumps(request), text=True,
                                  capture_output=True, timeout=5, env={})
        except (OSError, subprocess.SubprocessError):
            return _fail("claude_seat_helper_unavailable", ref)
        if proc.returncode != 0 or len(proc.stdout) > 4096:
            return _fail("claude_seat_helper_invalid", ref)
        answer = json.loads(proc.stdout)
        return Result(answer.get("ok") is True, answer["detail"],
                      dict(ref, seat=answer.get("seat")))


# run-record

class RunRecordVerifier:
    """``<runs_dir>/<run_id>/`` was produced for this item and exited 0."""

    kind = "run-record"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        run_id = ref.get("run_id")
        if "host" in ref:
            adapter = getattr(ctx, "spawner_records", None) or SpawnerRecordAdapter(None)
            return adapter.verify(item, ref)
        if not isinstance(run_id, str) or not run_id:
            return _fail("run-record requires a 'run_id'", ref)
        if ctx.runs is None or not ctx.runs.valid_run_id(run_id):
            return _fail(f"invalid run id: {run_id!r}", ref)

        try:
            job = ctx.runs.job(run_id)
        except FileNotFoundError:
            return _fail(f"run {run_id} has no job.json", ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"could not read run {run_id} ({type(exc).__name__})", ref)

        try:
            exit_record = ctx.runs.exit(run_id)
        except FileNotFoundError:
            return _fail(f"run {run_id} has no exit.json", ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"could not read run {run_id} ({type(exc).__name__})", ref)

        needles = [n for n in (item.get("key"), item.get("id")) if isinstance(n, str) and n]
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        legacy = "item_key" not in request
        if not legacy:
            item_key = request["item_key"]
            if not isinstance(item_key, str) or not item_key.strip():
                return _fail(f"run {run_id} request.item_key is present but "
                             "not a usable string", ref)
            if item_key not in needles:
                wanted = item.get("key") or item.get("id")
                return _fail(f"run {run_id} item_key={item_key!r} is not {wanted}", ref)
        else:
            haystack = " ".join(
                str(part)
                for part in (request.get("title"), job.get("task"))
                if part is not None
            )
            if not any(needle in haystack for needle in needles):
                wanted = item.get("key") or item.get("id")
                return _fail(f"run {run_id} does not mention {wanted}"
                             " (legacy title match)", ref)

        if exit_record.get("rc") != 0:
            return _fail(f"run {run_id} exited rc={exit_record.get('rc')!r}", ref)
        detail = f"run {run_id} for {item.get('key') or item.get('id')} rc=0"
        if legacy:
            detail += " (legacy title match)"
        return Result(True, detail, ref)


# semaphore-task

class SemaphoreTaskVerifier:
    """The Semaphore task finished ``success`` (and is the expected template).

    ``ref.readable`` is attested by the attacher and is not fetched or checked
    by the keeper; only the adapter allow list is a keeper-side fact.
    """

    kind = "semaphore-task"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        raw_task, raw_project = ref.get("task_id"), ref.get("project_id")
        if raw_task is None or raw_task == "":
            return _fail("semaphore-task requires a 'task_id'", ref)
        if raw_project is None or raw_project == "":
            return _fail("semaphore-task requires a 'project_id'", ref)
        task_id, project_id = positive_int(raw_task), positive_int(raw_project)
        if task_id is None:
            return _fail("semaphore-task 'task_id' must be a positive integer", ref)
        if project_id is None:
            return _fail("semaphore-task 'project_id' must be a positive integer", ref)
        template = ref.get("template")
        if template is None or template == "":
            return _fail("semaphore-task requires a 'template' (template id)", ref)
        want_template = positive_int(template)
        if want_template is None:
            return _fail("template name check unsupported; pass the template id", ref)
        if ctx.semaphore is None:
            return _fail("no semaphore adapter available", ref)
        if not (
            self._has_readable_copy(ref)
            or self._adapter_reports_readable(ctx.semaphore, project_id)
        ):
            return _fail(
                "semaphore-task evidence not readable by the reviewer host: "
                "attach ref.readable (an attested reviewer-readable copy; "
                "the keeper does not fetch it) or use a project on the keeper "
                "allow list",
                ref,
            )

        try:
            data = ctx.semaphore.get(task_id, project_id)
        except SemaphoreError as exc:
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        except Exception as exc:  # noqa: BLE001 - never surface the raw message
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        if not isinstance(data, dict):
            return _fail(f"semaphore task {task_id} returned no task object", ref)

        # Both IDs bind the response to this task.
        if positive_int(data.get("id")) != task_id:
            return _fail(f"semaphore response is for a different task than {task_id}", ref)
        if positive_int(data.get("project_id")) != project_id:
            return _fail(f"semaphore task {task_id} belongs to a different project", ref)

        status = data.get("status", data.get("state"))
        if str(status) != "success":
            return _fail(f"semaphore task {task_id} is not success (status={status!r})", ref)

        if positive_int(data.get("template_id")) != want_template:
            return _fail(f"semaphore task {task_id} used a different template", ref)
        return Result(True, f"semaphore task {task_id} succeeded", ref)

    @staticmethod
    def _has_readable_copy(ref: dict) -> bool:
        """``ref['readable']``: a non-empty http(s) URL or a bare artifact id."""
        value = ref.get("readable")
        if not isinstance(value, str):
            return False
        copy = value.strip()
        if not copy or any(ch.isspace() for ch in copy):
            return False
        for prefix in ("http://", "https://"):
            if copy.startswith(prefix):
                # URL: must have a netloc; the reviewer host follows it itself.
                return bool(copy[len(prefix):].split("/", 1)[0])
        # Artifact id: a bare token, never a path of any kind.
        return "/" not in copy and "\\" not in copy

    @staticmethod
    def _adapter_reports_readable(adapter: Any, project_id: int) -> bool:
        probe = getattr(adapter, "readable", None)
        if probe is None:
            return False
        try:
            return bool(probe(project_id)) if callable(probe) else bool(probe)
        except Exception:  # noqa: BLE001 - fail closed, never surface the error
            return False


# decision

class DecisionVerifier:
    """``| D-NNN |`` appears in the configured log on freshly fetched origin/main."""

    kind = "decision"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        decision_id = ref.get("id")
        if not isinstance(decision_id, str) or not decision_id:
            return _fail("decision requires an 'id' such as 'D-209'", ref)
        if ctx.decisions is None:
            return _fail("no decisions adapter available", ref)
        try:
            present = ctx.decisions.has(decision_id)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"could not read the decisions file ({type(exc).__name__})", ref)
        if not present:
            return _fail(f"decision {decision_id} is not recorded", ref)
        return Result(True, f"decision {decision_id} is recorded", ref)


# deploy proof (LE-MERGED-UNIT-NEVER-DEPLOYED)

_UNIT_RE = re.compile(r"^deploy/systemd/([^/]+\.(?:service|timer|socket|path))$")
_BIN_RE = re.compile(r"^rust/([^/]+)/src/(?:main\.rs|bin/([^/]+)\.rs)$")
_HOSTS_RE = re.compile(r"(?mi)^[ \t]*Deploy-Hosts:[ \t]*(.+?)[ \t]*$")


def deploy_targets(changes) -> list[str]:
    """Units and new binary entry points shipped by a diff of (status, path) pairs or added paths."""
    out = set()
    for change in changes or ():
        status, path = ("A", change) if isinstance(change, str) else change
        unit = _UNIT_RE.match(path)
        binary = _BIN_RE.match(path)
        if unit:
            out.add(unit.group(1))
        elif binary and status == "A":
            out.add("bin:" + (binary.group(2) or binary.group(1)))
    return sorted(out)


def deploy_host_names(values) -> list[str]:
    return sorted({host for value in values for host in re.split(r"[\s,]+", value)
                   if re.fullmatch(r"[A-Za-z0-9_.-]+", host)})


def deploy_hosts(item: dict) -> list[str]:
    """Hosts named by ``Deploy-Hosts: a, b`` lines of the item description or note."""
    return deploy_host_names(_HOSTS_RE.findall(f"{item.get('description') or ''}\n{item.get('note') or ''}"))


def deploy_missing(targets, hosts, rows) -> list[str]:
    """``host:unit`` pairs with no verified ``deploy`` evidence; ``(hosts unnamed)`` when none are named."""
    if not hosts:
        return ["(hosts unnamed)"]
    verified = [r["ref"] for r in rows if r.get("kind") == "deploy" and r.get("verified")]
    have = proven_pairs(verified)
    return [f"{host}:{unit}" for host in hosts for unit in targets
            if (host, unit) not in have]


def _deploy_commit_time(item, ctx, unit, timer):
    from .context_sources import MERGE_REPO_LINE

    text = f"{item.get('description') or ''}\n{item.get('note') or ''}"
    projects = {match[1] for line in text.splitlines()
                if (match := MERGE_REPO_LINE.fullmatch(line))}
    if len(projects) > 1 or ctx.git is None:
        return None
    project = next(iter(projects), item.get("project"))
    repo = ctx.git.repo_path(project)
    if not repo:
        return None
    # A merge is the deployed change even when the topic commit is much older.
    proc = ctx.git._run(repo, ["log", "-1", "--first-parent", "--format=%ct",
                              "--end-of-options", ctx.git.repo_ref(project), "--",
                              f":(literal)deploy/systemd/{unit}",
                              f":(literal)deploy/systemd/{timer}"])
    return as_epoch(proc.stdout.strip()) if proc.returncode == 0 else None


def _exec_main_exit_time(value):
    epoch = as_epoch(value)
    if epoch is None and isinstance(value, str):
        # Also accept the C-locale output of systemctl --utc show.
        try:
            epoch = datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S UTC").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return epoch if epoch is not None and math.isfinite(epoch) else None


class DeployVerifier:
    """Check a host's active unit, installed binary, or recent successful oneshot."""

    kind = "deploy"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        host, unit, state = ref.get("host"), ref.get("unit"), ref.get("is_active")
        if not (isinstance(host, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", host)
                and isinstance(unit, str) and unit):
            return _fail("deploy requires 'host' and 'unit'", ref)
        timer = oneshot_timer(ref)
        if timer is not None:
            if "/" in unit or unit == ".service":
                return _fail("deploy oneshot requires a service unit name", ref)
            if ref.get("result") != "success":
                return _fail(f"deploy {unit} on {host} must report result=success", ref)
            exited = _exec_main_exit_time(ref.get("exec_main_exit_timestamp"))
            if exited is None or exited > ctx.now:
                return _fail(f"deploy {unit} on {host} requires a valid exec_main_exit_timestamp", ref)
            deployed = _deploy_commit_time(item, ctx, unit, timer)
            if deployed is None or exited <= deployed:
                return _fail(f"deploy {unit} on {host} requires a successful run after the deploy commit", ref)
            return Result(True, f"{unit} succeeded on {host}; deploy requires active {timer}", ref)
        want = "installed" if unit.startswith("bin:") else "active"
        if state != want:
            return _fail(f"deploy {unit} on {host} must report is_active={want}", ref)
        return Result(True, f"{unit} {want} on {host}", ref)


# registry and factory

VERIFIERS: dict[str, Any] = {
    verifier.kind: verifier
    for verifier in (
        CommitCiVerifier(),
        LegacyCommitVerifier(),
        RunRecordVerifier(),
        ClaudeSeatVerifier(),
        SemaphoreTaskVerifier(),
        DecisionVerifier(),
        ErrorRecordVerifier(),
        DeployVerifier(),
    )
}


def verify(kind: str, item: dict, ref: dict, ctx: Any) -> Result:
    """Dispatch by evidence kind; an unknown kind is a validation failure."""
    try:
        verifier = VERIFIERS.get(kind)
    except Exception as exc:  # noqa: BLE001 - e.g. an unhashable kind
        return Result(False, type(exc).__name__, _ref(ref))
    if verifier is None:
        return Result(False, f"unknown evidence kind: {kind}", _ref(ref))
    return verifier.verify(item, ref, ctx)


def _cfg(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _principals(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return ()
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                result.append(cleaned)
    return tuple(result)


def default_context(config: Any, *, now: float | None = None) -> Context:
    """Build adapters lazily from the fields documented by keeper.config."""
    forgejo = None
    forgejo_url = _cfg(config, "forgejo_url")
    forgejo_token_file = _cfg(config, "forgejo_token_file")
    if forgejo_url and forgejo_token_file:
        forgejo = ForgejoStatusAdapter(str(forgejo_url), str(forgejo_token_file))
    context = Context(
        git=GitAdapter(_cfg(config, "repos", {}) or {}, merge_scan=_cfg(config, "merge_scan")),
        ci=CIAdapter(_cfg(config, "ci", {}) or {}, forgejo=forgejo),
        runs=RunRecordAdapter(_cfg(config, "runs_dir")),
        semaphore=SemaphoreAdapter(),
        decisions=DecisionsAdapter(_cfg(config, "decisions_file"), from_origin=True),
        claude_projects_dir=_cfg(config, "claude_projects_dir"),
        spawner_records=SpawnerRecordAdapter.for_keeper(_cfg(config, "spawner_records_dir"), _cfg(config, "runs_dir")),
        now=time.time() if now is None else float(now),
        claim_time=as_epoch(_cfg(config, "claim_time")),
        merge_principals=_principals(_cfg(config, "merge_principals")),
        skew=float(_cfg(config, "skew", DEFAULT_SKEW_S)),
    )
    return context
