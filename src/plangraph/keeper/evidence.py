"""Pluggable evidence verifiers (docs/KEEPER.md, "Evidence verifiers").

Four kinds, one registry, one factory for the real adapters. Every verifier has
the same shape ``verify(item, ref, ctx) -> Result`` and reads all I/O through the
adapters on ``ctx`` (``git``, ``ci``, ``runs``, ``semaphore``, ``decisions``),
which is what lets the tests run with fakes and no network. ``ctx.claim_time`` is
the claim instant used for the "commit newer than the claim" rule; when it is
missing the verifier fails closed.

Nothing here prints or returns a secret. Adapter error text is reduced to the
exception *type* before it reaches a :class:`Result`.
"""

from __future__ import annotations

import time
from typing import Any

from .evidence_ci import CIAdapter
from .evidence_decisions import DecisionsAdapter
from .evidence_git import GitAdapter
from .evidence_runs import RunRecordAdapter
from .evidence_semaphore import SemaphoreAdapter, SemaphoreError
from .evidence_types import Context, Result, as_epoch

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


def _template_matches(data: dict, expected: Any) -> bool:
    wanted = str(expected)
    candidates: list[Any] = []
    for key in ("template_id", "template_name"):
        if key in data:
            candidates.append(data[key])
    template = data.get("template")
    if isinstance(template, dict):
        for key in ("id", "name"):
            if key in template:
                candidates.append(template[key])
    elif template is not None:
        candidates.append(template)
    return any(str(candidate) == wanted for candidate in candidates)


# --------------------------------------------------------------------------- #
# commit+ci
# --------------------------------------------------------------------------- #

class CommitCiVerifier:
    """A commit on ``main`` made for this item, dated after the claim, CI green."""

    kind = "commit+ci"

    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        commit = ref.get("commit")
        if not isinstance(commit, str) or not commit:
            return _fail("commit+ci requires a 'commit' sha", ref)

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
            if not ctx.git.is_ancestor_of_main(repo, commit):
                return _fail(f"commit {commit} is not an ancestor of main", ref)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error checking main ancestry ({type(exc).__name__})", ref)

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
            commit_epoch = ctx.git.commit_time(repo, commit)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"git error reading the commit time ({type(exc).__name__})", ref)
        if commit_epoch is None:
            return _fail(f"could not read the commit time of {commit}", ref)
        if commit_epoch < claim_epoch:
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
        not_green = [run for run in runs if str(run.get("conclusion", "")).lower() != "success"]
        if not_green:
            names = ", ".join(str(run.get("name", "?")) for run in not_green[:3])
            return _fail(f"CI is not green for {commit} ({names})", ref)
        return Result(True, f"commit {commit} on main with Plan-Item and green CI", ref)


# --------------------------------------------------------------------------- #
# run-record
# --------------------------------------------------------------------------- #

class RunRecordVerifier:
    """``<runs_dir>/<run_id>/`` was produced for this item and exited 0."""

    kind = "run-record"

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

    def verify(self, item: dict, ref: dict, ctx: Any) -> Result:
        ref = _ref(ref)
        task_id = ref.get("task_id")
        if task_id is None or task_id == "":
            return _fail("semaphore-task requires a 'task_id'", ref)
        if ctx.semaphore is None:
            return _fail("no semaphore adapter available", ref)

        try:
            data = ctx.semaphore.get(task_id)
        except SemaphoreError as exc:
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        except Exception as exc:  # noqa: BLE001 - never surface the raw message
            return _fail(f"semaphore fetch failed ({type(exc).__name__})", ref)
        if not isinstance(data, dict):
            return _fail(f"semaphore task {task_id} returned no task object", ref)

        status = data.get("status", data.get("state"))
        if str(status) != "success":
            return _fail(f"semaphore task {task_id} is not success (status={status!r})", ref)

        template = ref.get("template")
        if template is not None and not _template_matches(data, template):
            return _fail(f"semaphore task {task_id} used a different template", ref)
        return Result(True, f"semaphore task {task_id} succeeded", ref)


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #

class DecisionVerifier:
    """``| D-NNN |`` appears in the configured DECISIONS.md."""

    kind = "decision"

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
        RunRecordVerifier(),
        SemaphoreTaskVerifier(),
        DecisionVerifier(),
    )
}


def verify(kind: str, item: dict, ref: dict, ctx: Any) -> Result:
    """Dispatch by evidence kind; an unknown kind is a validation failure."""
    verifier = VERIFIERS.get(kind)
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
    ``none``), ``runs_dir`` and ``decisions_file``. Nothing here performs I/O or
    connects anywhere; the adapters only read when a verifier calls them.
    """
    return Context(
        git=GitAdapter(_cfg(config, "repos", {}) or {}),
        ci=CIAdapter(_cfg(config, "ci", {}) or {}),
        runs=RunRecordAdapter(_cfg(config, "runs_dir")),
        semaphore=SemaphoreAdapter(),
        decisions=DecisionsAdapter(_cfg(config, "decisions_file")),
        now=time.time() if now is None else float(now),
        claim_time=as_epoch(_cfg(config, "claim_time")),
    )
