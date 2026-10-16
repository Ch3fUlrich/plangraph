"""Pluggable evidence verifiers (docs/KEEPER.md, "Evidence verifiers").

Four kinds, one registry, one factory for the real adapters. Every verifier has
the same shape ``verify(item, ref, ctx) -> Result`` and reads all I/O through the
adapters on ``ctx`` (``git``, ``ci``, ``runs``, ``semaphore``, ``decisions``),
which is what lets the tests run with fakes and no network. ``ctx.claim_time`` is
the claim instant used for the "commit newer than the claim" rule; when it is
missing the verifier fails closed.

A ``commit+ci`` ref must be a full lowercase hex sha (40 for sha1, 64 for
sha256); anything else is rejected *before* any git or gh call, so a ref can
never become a git option. Both the author (``%at``) and committer (``%ct``)
dates must be at or after ``claim_time`` minus ``ctx.skew`` (default 120 s).
Those dates are **advisory**: git lets an author forge both timestamps, so the
binding evidence is the ``Plan-Item`` trailer, the commit being an ancestor of
``main`` and a green CI result on that exact sha.

Every verifier is wrapped by :func:`_guarded`: an unexpected exception (a
subprocess timeout, an ``OSError``, a JSON error) becomes
``Result(ok=False, detail=<exception class name>)`` -- it never raises and never
passes. Detail text is reduced to the exception *type*; adapters never return a
secret in it.
"""

from __future__ import annotations

import functools
import time
from typing import Any

from .evidence_ci import CIAdapter
from .evidence_decisions import DecisionsAdapter
from .evidence_git import GitAdapter, LegacyCommitVerifier
from .evidence_runs import RunRecordAdapter
from .evidence_semaphore import SemaphoreAdapter, SemaphoreError, positive_int
from .item_fixes import ErrorRecordVerifier
from .evidence_types import (
    DEFAULT_SKEW_S,
    Context,
    Result,
    as_epoch,
    valid_commit_ref,
)

__all__ = [
    "CommitCiVerifier",
    "RunRecordVerifier",
    "SemaphoreTaskVerifier",
    "DecisionVerifier",
    "VERIFIERS",
    "default_context",
    "verify",
    "Result",
    "Context",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

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


def _skew(ctx: Any) -> float:
    """The configured date tolerance, defaulting to :data:`DEFAULT_SKEW_S`."""
    try:
        value = float(getattr(ctx, "skew", DEFAULT_SKEW_S))
    except (TypeError, ValueError):
        return DEFAULT_SKEW_S
    return value if value >= 0 else DEFAULT_SKEW_S


def _commit_times(git: Any, repo: str, commit: str) -> tuple[float | None, float | None]:
    """(author, committer) epochs; falls back to a single ``commit_time``."""
    fn = getattr(git, "commit_times", None)
    if fn is not None:
        result = fn(repo, commit)
        if isinstance(result, (tuple, list)) and len(result) == 2:
            return result[0], result[1]
    single = git.commit_time(repo, commit)
    return single, single


# --------------------------------------------------------------------------- #
# commit+ci
# --------------------------------------------------------------------------- #

def _ci_conclusion(runs: Any, commit: str) -> str | None:
    """Newest-run-per-name CI verdict: None means ok, else the fail detail."""
    newest: dict[str, tuple[tuple[int, str], dict]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name", "?"))
        rid = run.get("id")
        rid_int = rid if isinstance(rid, int) and not isinstance(rid, bool) else 0
        started = run.get("started_at")
        started_str = str(started) if started is not None else ""
        key = (rid_int, started_str)
        prev = newest.get(name)
        if prev is None or key > prev[0]:
            newest[name] = (key, run)
    if not newest:
        return None
    pending: list[str] = []
    failures: list[tuple[str, str]] = []
    seen: list[str] = []
    for name in sorted(newest):
        run = newest[name][1]
        conc_raw = run.get("conclusion")
        if conc_raw is None or (
            isinstance(conc_raw, str) and conc_raw.strip() == ""
        ):
            pending.append(name)
            continue
        conc = str(conc_raw).strip()
        if conc == "":
            pending.append(name)
            continue
        if "status" in run and str(run.get("status")).strip().lower() != "completed":
            pending.append(name)
            continue
        norm = conc.lower()
        seen.append(norm)
        if norm in ("success", "skipped", "neutral"):
            continue
        failures.append((name, str(conc_raw)))
    if failures:
        inner = ", ".join(f"{n}={c}" for n, c in failures)
        return f"CI is not green for {commit} ({inner})"
    if pending:
        return f"CI pending for {commit} ({', '.join(pending)}), retry later"
    if seen and all(c == "skipped" for c in seen):
        return f"CI only skipped for {commit}"
    return None


class CommitCiVerifier:
    """A commit on ``main`` made for this item, dated after the claim, CI green."""

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

        project = item.get("project")
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

        try:
            target_ref = ctx.git.repo_ref(project) if hasattr(ctx.git, "repo_ref") else "main"
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
        trailer = _trailer(trailers, "Plan-Item")
        if not trailer or trailer not in wanted:
            label = item.get("key") or item.get("id")
            return _fail(
                f"commit {commit} does not carry trailer 'Plan-Item: {label}'", ref
            )

        claim_epoch = as_epoch(getattr(ctx, "claim_time", None))
        if claim_epoch is None:
            return _fail("no claim time available to compare the commit time", ref)
        try:
            author_epoch, committer_epoch = _commit_times(ctx.git, repo, commit)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error reading the commit time ({type(exc).__name__})", ref)
        if author_epoch is None or committer_epoch is None:
            return _fail(f"could not read the commit time of {commit}", ref)
        threshold = claim_epoch - _skew(ctx)
        if author_epoch < threshold or committer_epoch < threshold:
            return _fail(f"commit {commit} predates the claim", ref)

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


# --------------------------------------------------------------------------- #
# run-record
# --------------------------------------------------------------------------- #

class RunRecordVerifier:
    """``<runs_dir>/<run_id>/`` was produced for this item and exited 0."""

    kind = "run-record"

    @_guarded
    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        run_id = ref.get("run_id")
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
        haystack = " ".join(
            str(part)
            for part in (request.get("title"), job.get("task"))
            if part is not None
        )
        if not any(needle in haystack for needle in needles):
            wanted = item.get("key") or item.get("id")
            return _fail(f"run {run_id} does not mention {wanted}", ref)

        if exit_record.get("rc") != 0:
            return _fail(f"run {run_id} exited rc={exit_record.get('rc')!r}", ref)
        return Result(True, f"run {run_id} for {item.get('key') or item.get('id')} rc=0", ref)


# --------------------------------------------------------------------------- #
# semaphore-task
# --------------------------------------------------------------------------- #

class SemaphoreTaskVerifier:
    """The Semaphore task finished ``success`` (and is the expected template)."""

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

        try:
            data = ctx.semaphore.get(task_id, project_id)
        except SemaphoreError as exc:
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        except Exception as exc:  # noqa: BLE001 - never surface the raw message
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        if not isinstance(data, dict):
            return _fail(f"semaphore task {task_id} returned no task object", ref)

        # Both ids are required in the answer: a reply that omits them is not
        # proof about *this* task (fail closed, seat finding).
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


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #

class DecisionVerifier:
    """``| D-NNN |`` appears in the configured DECISIONS.md."""

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


# --------------------------------------------------------------------------- #
# registry and factory
# --------------------------------------------------------------------------- #

VERIFIERS: dict[str, Any] = {
    verifier.kind: verifier
    for verifier in (
        CommitCiVerifier(),
        LegacyCommitVerifier(),
        RunRecordVerifier(),
        SemaphoreTaskVerifier(),
        DecisionVerifier(),
        ErrorRecordVerifier(),
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


def default_context(config: Any, *, now: float | None = None) -> Context:
    """Build the real adapters from a keeper config mapping.

    Expected keys: ``repos`` (project → path), ``ci`` (project → slug/path/
    ``none``), ``runs_dir``, ``decisions_file`` and optional ``skew`` seconds.
    Nothing here performs I/O or connects anywhere; the adapters only read when
    a verifier calls them.
    """
    return Context(
        git=GitAdapter(_cfg(config, "repos", {}) or {}),
        ci=CIAdapter(_cfg(config, "ci", {}) or {}),
        runs=RunRecordAdapter(_cfg(config, "runs_dir")),
        semaphore=SemaphoreAdapter(),
        decisions=DecisionsAdapter(_cfg(config, "decisions_file")),
        now=time.time() if now is None else float(now),
        claim_time=as_epoch(_cfg(config, "claim_time")),
        skew=float(_cfg(config, "skew", DEFAULT_SKEW_S)),
    )
