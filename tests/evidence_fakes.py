"""Shared fakes for the evidence verifier tests (not collected by pytest).

A single fake per adapter, injected on a lightweight ``ctx`` stand-in, so the
verifier tests need no git, no network and no filesystem.
"""

from __future__ import annotations

from types import SimpleNamespace

from plangraph.keeper import evidence as ev

SHA = "a" * 40
OTHER_SHA = "b" * 40
CLAIM = 1_700_000_000.0
ITEM = {"project": "routing", "id": "T2-ONEPATH", "key": "routing:T2-ONEPATH"}
HEAD_SHA = "c" * 40
MERGE_SHA = "d" * 40
MERGE_EMAIL = "merger@example.invalid"


class FakeGit:
    def __init__(
        self,
        repo: str | None = "/repos/routing",
        exists: bool = True,
        ancestor: bool = True,
        trailers: dict | None = None,
        trailers_by_sha: dict | None = None,
        branch_trailers_by_sha: dict | None = None,
        commit_time: float | None = CLAIM + 100,
        error: Exception | None = None,
        author_time: float | None = None,
        commit_time_error: Exception | None = None,
        head: str | None = HEAD_SHA,
        head_error: Exception | None = None,
        merge: tuple[str, str] | None = (MERGE_SHA, MERGE_EMAIL),
        merge_error: Exception | None = None,
        descends: bool = True,
        floor_error: Exception | None = None,
    ):
        self.repo = repo
        self.exists = exists
        self.ancestor = ancestor
        self.message_trailers = {"Plan-Item": ITEM["key"]} if trailers is None else trailers
        # Per-commit trailers for the seat-coverage tests; any sha not listed
        # falls back to ``trailers`` above.
        self.trailers_by_sha = trailers_by_sha or {}
        # Merge/phase evidence shas: one dict per BRANCH commit (the adapter's
        # ``writer_trailers`` result); any sha not listed falls back to a
        # single-element list holding ``trailers`` above.
        self.branch_trailers_by_sha = branch_trailers_by_sha or {}
        self.ctime = commit_time
        self.atime = commit_time if author_time is None else author_time
        self.error = error
        self.commit_time_error = commit_time_error
        self.head = head
        self.head_error = head_error
        self.merge = merge
        self.merge_error = merge_error
        self.descends = descends
        self.floor_error = floor_error
        self.repo_path_calls: list[str] = []

    def repo_path(self, project):
        self.repo_path_calls.append(project)
        if self.error:
            raise self.error
        return self.repo

    def repo_ref(self, project=None, *args, **kwargs):
        return "main"

    def commit_exists(self, repo, sha):
        return self.exists

    def is_ancestor(self, repo, sha, ref):
        if isinstance(ref, str) and len(ref) == 40 and all(c in "0123456789abcdefABCDEF" for c in ref):
            if self.floor_error:
                raise self.floor_error
            return self.descends
        return self.ancestor

    def is_ancestor_of_main(self, repo, sha):
        return self.ancestor

    def trailers(self, repo, sha):
        if sha in self.trailers_by_sha:
            return dict(self.trailers_by_sha[sha])
        return dict(self.message_trailers)

    def writer_trailers(self, repo, sha):
        """One dict per commit whose trailers name the writers (ADDENDUM).

        A merge/phase evidence sha carries the branch commits' trailers; any
        other sha is a single commit whose own trailers stand for it.
        """
        if sha in self.branch_trailers_by_sha:
            return [dict(d) for d in self.branch_trailers_by_sha[sha]]
        return [self.trailers(repo, sha)]

    def commit_times(self, repo, sha):
        if self.commit_time_error:
            raise self.commit_time_error
        return (self.atime, self.ctime)

    def commit_time(self, repo, sha):
        if self.commit_time_error:
            raise self.commit_time_error
        return self.ctime

    def head_sha(self, repo, ref="main"):
        if self.head_error:
            raise self.head_error
        return self.head

    def merge_commit_of(self, repo, sha, ref="main"):
        if self.merge_error:
            raise self.merge_error
        return self.merge


class FakeCI:
    def __init__(self, mode: str | None = "owner/repo", runs=None, error: Exception | None = None):
        self._mode = mode
        self.runs = runs if runs is not None else [{"name": "build", "head_sha": SHA, "conclusion": "success"}]
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def mode(self, project):
        return self._mode

    def check_runs(self, repo_slug_or_path, sha):
        self.calls.append((repo_slug_or_path, sha))
        if self.error:
            raise self.error
        return list(self.runs)


class FakeRuns:
    def __init__(self, valid: bool = True, job: dict | None = None, exit: dict | None = None,
                 job_error: Exception | None = None, exit_error: Exception | None = None):
        self.valid = valid
        self._job = job if job is not None else {"request": {"title": "do " + ITEM["key"]}, "task": ""}
        self._exit = exit if exit is not None else {"rc": 0}
        self.job_error = job_error
        self.exit_error = exit_error

    def valid_run_id(self, run_id):
        return self.valid

    def job(self, run_id):
        if self.job_error:
            raise self.job_error
        return self._job

    def exit(self, run_id):
        if self.exit_error:
            raise self.exit_error
        return self._exit


class FakeSemaphore:
    def __init__(self, data: dict | None = None, error: Exception | None = None,
                 readable: bool = True):
        self.data = data if data is not None else {"status": "success", "id": 5, "project_id": 1, "template_id": 7}
        self.error = error
        self.readable = readable

    def get(self, task_id, project_id):
        self.calls = getattr(self, 'calls', []) + [(task_id, project_id)]
        if self.error:
            raise self.error
        return self.data


class FakeDecisions:
    def __init__(self, present: set | None = None, error: Exception | None = None,
                 waiving: set | None = None):
        self.present = present if present is not None else {"D-209"}
        self.error = error
        # (decision_id, key) pairs whose D-row names ``<key>`` in its DECISION
        # cell (3rd column) as a whole ``Waives:``/``Closes:`` token (D-276);
        # ``None`` means "a recorded id waives every key it is asked about".
        self.waiving = waiving

    def has(self, decision_id):
        if self.error:
            raise self.error
        return decision_id in self.present

    def waives(self, decision_id, key):
        if self.error:
            raise self.error
        if self.waiving is None:
            return decision_id in self.present
        return (decision_id, key) in self.waiving


def make_ctx(**over):
    base = dict(
        git=FakeGit(),
        ci=FakeCI(),
        runs=FakeRuns(),
        semaphore=FakeSemaphore(),
        decisions=FakeDecisions(),
        now=CLAIM + 50,
        claim_time=CLAIM,
        merge_principals=(),
        bind_floor_sha=None,
        bind_floor_required=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def verify(kind, ref, ctx=None, item=None):
    return ev.VERIFIERS[kind].verify(item or ITEM, ref, ctx or make_ctx())
