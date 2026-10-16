You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review a HIGH-risk change to a CI-evidence verifier (keeper commit+ci). Intent: for workflow_dispatch runs, the newest COMPLETED run per (workflow, job name) on a sha wins across check suites; an older cancelled run is ignored once a newer completed run exists; a newer failure/cancelled still blocks; PR-only suites unchanged; PR+dispatch with same (workflow,job) merge into one group. Adapter backfills run["workflow"] from GET actions/runs?head_sha= (join check_suite.id == check_suite_id). Real fields: check-run {id,name,status,conclusion,started_at,head_sha,app.slug,check_suite.id}; workflow_run {id,check_suite_id,event,name,workflow_id,run_attempt,status,conclusion}. Check-run ids are not chronological across reruns, so ranking is (workflow.run_id, check-run id, started_at).
Vectors (report per file:function, with a concrete failing input, or "no finding"):
V1 evidence.py:_ci_conclusion — false-green: a real failure masked (e.g. group-key collisions, wf_id 0/None, missing run_id ranks 0, completed-but-older failure hidden by a newer success that ran different work, skipped-only).
V2 evidence.py:_ci_conclusion — PR path regression: PR-only and non-workflow runs must behave exactly as before incl. message formats.
V3 evidence_ci.py:check_runs/_page_all — API field honesty: fake vs real gh contract, truncation/pagination refusal, suite->workflow join, unmatched suites, injection via head_sha query, second-call failure handling.


NOTE: second pass on the FINAL head de6d7aa552230f0fd943840b6527754aab2d8905. Earlier findings (wf_id 0 collision, missing run_id ranking, check_suite_id None mis-join) were fixed; the 'skipped rerun beats older failure' case is intended (newest completed wins). Re-verify and look for anything new.
=== DIFF ===
diff --git a/src/plangraph/keeper/evidence.py b/src/plangraph/keeper/evidence.py
index 36d40504..a97dbc09 100644
--- a/src/plangraph/keeper/evidence.py
+++ b/src/plangraph/keeper/evidence.py
@@ -120,8 +120,30 @@ def _commit_times(git: Any, repo: str, commit: str) -> tuple[float | None, float
 # --------------------------------------------------------------------------- #
 
 def _ci_conclusion(runs: Any, commit: str) -> str | None:
-    """Newest-run-per-(suite, app, name) CI verdict: None means ok, else the fail detail. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
-    newest: dict[tuple[Any, Any, str], tuple[tuple[int, str], dict]] = {}
+    """Newest-run-per-group CI verdict: None means ok, else the fail detail. Plain groups are (suite, app, name); a workflow_dispatch run forms a ("wf", workflow_id, app, name) group across suites that a matching PR run joins; a 'workflow' dict lacking a positive workflow_id or run_id counts as no 'workflow' at all. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
+
+    def _id_int(value: Any) -> int:
+        # Same coercion as check-run ids: bool is not an id, digit strings rank as ints.
+        if isinstance(value, bool):
+            return 0
+        if isinstance(value, int):
+            return value
+        if isinstance(value, str) and value.isdigit():
+            return int(value)
+        return 0
+
+    def _hashable(value: Any) -> Any:
+        # gh hands us ints; an unhashable id from a fake is then simply no key.
+        try:
+            hash(value)
+        except TypeError:
+            return None
+        return value
+
+    # Pass 1: parse the runs and collect the (workflow, app, name) keys that have
+    # a workflow_dispatch run, so a PR run with the same key joins that wf group.
+    parsed: list[dict] = []
+    dispatch_keys: set = set()
     for run in runs:
         if not isinstance(run, dict):
             continue
@@ -129,66 +151,116 @@ def _ci_conclusion(runs: Any, commit: str) -> str | None:
         suite: Any = None
         cs = run.get("check_suite")
         if isinstance(cs, dict) and "id" in cs:
-            sid = cs["id"]
-            try:
-                hash(sid)
-            except TypeError:
-                pass
-            else:
-                suite = sid
+            suite = _hashable(cs["id"])
         app: Any = None
         ap = run.get("app")
         if isinstance(ap, dict) and "slug" in ap:
-            slug = ap["slug"]
-            try:
-                hash(slug)
-            except TypeError:
-                pass
-            else:
-                app = slug
-        rid = run.get("id")
-        if isinstance(rid, bool):
-            rid_int = 0
-        elif isinstance(rid, int):
-            rid_int = rid
-        elif isinstance(rid, str) and rid.isdigit():
-            rid_int = int(rid)
-        else:
-            rid_int = 0
+            app = _hashable(ap["slug"])
+        rid_int = _id_int(run.get("id"))
         started = run.get("started_at")
         started_str = str(started) if started is not None else ""
-        key = (rid_int, started_str)
-        gkey = (suite, app, name)
-        prev = newest.get(gkey)
-        if prev is None or key > prev[0]:
-            newest[gkey] = (key, run)
-    if not newest:
+        wf = run.get("workflow")
+        wf_id = _id_int(wf.get("workflow_id")) if isinstance(wf, dict) else 0
+        wf_rid = _id_int(wf.get("run_id")) if isinstance(wf, dict) else 0
+        # A 'workflow' dict only counts with a real positive workflow_id AND a
+        # positive run_id (bool is not an id; digit strings rank as ints).
+        # Anything else -- missing, None, bool, junk -- is handled exactly like
+        # a missing key, so junk ids cannot merge unrelated runs into one
+        # ("wf", 0, ...) group where an unrelated success would mask a failure,
+        # and a run with no run_id can never be ranked inside a wf group.
+        in_wf = wf_id > 0 and wf_rid > 0
+        event: str | None = None
+        if in_wf:
+            raw_event = wf.get("event")
+            event = str(raw_event) if raw_event is not None else None
+            if event == "workflow_dispatch":
+                dispatch_keys.add((wf_id, app, name))
+        else:
+            wf_id = 0
+            wf_rid = 0
+        parsed.append({"name": name, "suite": suite, "app": app, "run": run,
+                       "event": event, "wf_id": wf_id, "in_wf": in_wf,
+                       "key": (rid_int, started_str), "wf_rid": wf_rid})
+
+    # Pass 2: group. A wf group ranks by (workflow.run_id, check-run id,
+    # started_at) because check-run ids are NOT chronological across runs: a
+    # rerun gets new ids that can be lower or higher than other runs' ids.
+    groups: dict[Any, dict] = {}
+    for rec in parsed:
+        is_wf = rec["event"] == "workflow_dispatch"
+        if not is_wf and rec["in_wf"]:
+            # Any other event (pull_request, ...) keeps its suite key unless a
+            # dispatch exists for the same workflow, app and check name.
+            is_wf = (rec["wf_id"], rec["app"], rec["name"]) in dispatch_keys
+        if is_wf:
+            gkey: Any = ("wf", rec["wf_id"], rec["app"], rec["name"])
+            rank: Any = (rec["wf_rid"], rec["key"][0], rec["key"][1])
+            sort: Any = rec["wf_id"]
+            suffix: Any = f"workflow {rec['wf_id']}"
+        else:
+            gkey = (rec["suite"], rec["app"], rec["name"])
+            rank = rec["key"]
+            sort = rec["suite"]
+            suffix = f"suite {rec['suite']}" if rec["suite"] is not None else None
+        g = groups.get(gkey)
+        if g is None:
+            g = {"name": rec["name"], "app": rec["app"], "wf": is_wf,
+                 "sort": sort, "suffix": suffix, "runs": []}
+            groups[gkey] = g
+        g["runs"].append((rank, rec["run"]))
+    if not groups:
         return f"no CI check-runs found for {commit}"
+
+    def _completed(run: dict) -> bool:
+        # Non-empty conclusion and status absent-or-completed (legacy fakes).
+        conc_raw = run.get("conclusion")
+        if conc_raw is None or str(conc_raw).strip() == "":
+            return False
+        return not ("status" in run and str(run.get("status")).strip().lower() != "completed")
+
     pending_groups: list[tuple[str, Any, Any]] = []
     failure_groups: list[tuple[str, Any, Any, str]] = []
     seen: list[str] = []
-    for gkey in sorted(newest, key=lambda k: (k[2], str(k[0]), str(k[1]))):
-        run = newest[gkey][1]
-        name = gkey[2]
-        suite, app = gkey[0], gkey[1]
+    for gkey in sorted(groups, key=lambda k: (groups[k]["name"], str(groups[k]["sort"]), str(groups[k]["app"]))):
+        g = groups[gkey]
+        if g["wf"]:
+            # Only completed runs compete inside a wf group; a non-completed run
+            # ranked newer than the newest completed one (or none at all) is pending.
+            best: tuple[Any, dict] | None = None
+            for rank, run in g["runs"]:
+                if _completed(run) and (best is None or rank > best[0]):
+                    best = (rank, run)
+            if best is None or any(
+                rank > best[0] and not _completed(run) for rank, run in g["runs"]
+            ):
+                pending_groups.append((g["name"], g["suffix"], g["app"]))
+                continue
+            run = best[1]
+        else:
+            # Plain groups keep today's rule: the newest run alone is evaluated.
+            best = None
+            for rank, run in g["runs"]:
+                if best is None or rank > best[0]:
+                    best = (rank, run)
+            run = best[1]
         conc_raw = run.get("conclusion")
         if conc_raw is None or (
             isinstance(conc_raw, str) and conc_raw.strip() == ""
         ):
-            pending_groups.append((name, suite, app))
+            pending_groups.append((g["name"], g["suffix"], g["app"]))
             continue
         conc = str(conc_raw).strip()
         if conc == "":
-            pending_groups.append((name, suite, app))
+            pending_groups.append((g["name"], g["suffix"], g["app"]))
             continue
         if "status" in run and str(run.get("status")).strip().lower() != "completed":
-            pending_groups.append((name, suite, app))
+            pending_groups.append((g["name"], g["suffix"], g["app"]))
             continue
         norm = conc.lower()
         seen.append(norm)
         if norm in ("success", "skipped", "neutral"):
             continue
-        failure_groups.append((name, suite, app, str(conc_raw)))
+        failure_groups.append((g["name"], g["suffix"], g["app"], str(conc_raw)))
     if failure_groups:
         from collections import Counter
 
@@ -198,7 +270,7 @@ def _ci_conclusion(runs: Any, commit: str) -> str | None:
             if counts[n] <= 1:
                 return n
             if s is not None:
-                return f"{n} (suite {s})"
+                return f"{n} ({s})"
             if a is not None:
                 return f"{n} ({a})"
             return n
@@ -216,7 +288,7 @@ def _ci_conclusion(runs: Any, commit: str) -> str | None:
             if counts[n] <= 1:
                 return n
             if s is not None:
-                return f"{n} (suite {s})"
+                return f"{n} ({s})"
             if a is not None:
                 return f"{n} ({a})"
             return n
diff --git a/src/plangraph/keeper/evidence_ci.py b/src/plangraph/keeper/evidence_ci.py
index 8e34dc39..f588793e 100644
--- a/src/plangraph/keeper/evidence_ci.py
+++ b/src/plangraph/keeper/evidence_ci.py
@@ -24,6 +24,19 @@ __all__ = ["CIAdapter"]
 _SLUG_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
 
 
+def _pos_int(value: object) -> int | None:
+    """A real positive int id, else None.
+
+    Shared by both halves of the workflow backfill (workflow-run suite ids and
+    check-run suite ids, then the run and workflow ids behind the 'workflow'
+    key): bool is not an id (``True == 1`` would hijack suite 1) and a digit
+    string is not an id here either — GitHub sends ints.
+    """
+    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
+        return value
+    return None
+
+
 def slug_from_remote(url: str) -> str | None:
     """Turn a git remote URL into ``owner/repo`` (``None`` if unrecognisable)."""
     url = url.strip()
@@ -76,19 +89,95 @@ class CIAdapter:
         into a slug through its ``origin`` remote. The sha and the slug are both
         validated before any process runs; the API path is assembled from those
         validated components only, so no user text can inject a query.
+        When any check-run comes from GitHub Actions, the workflow runs for the
+        sha are fetched through the same gated paging and backfilled onto the
+        check-runs whose check-suite id matches (``workflow`` key); only a
+        positive int suite id counts on either side, and the key is written
+        only when the matched run also has a positive int id and workflow_id
+        and a string event.
         Raises on a non-zero ``gh`` exit or unparseable output — the verifier
         catches and sanitises that.
         """
         if not valid_commit_ref(sha):
             raise ValueError("check-runs requires a full lowercase commit sha")
         owner, repo = self._validated_slug(self._slug(repo_slug_or_path))
-        base = "repos/{}/{}/commits/{}/check-runs".format(
-            quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
+        # Both endpoints are assembled from these validated components only,
+        # so neither the slug nor the sha can inject a query or a path segment.
+        quoted = quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
+        collected = self._page_all(
+            "repos/{}/{}/commits/{}/check-runs".format(*quoted),
+            "",
+            "check_runs",
+            "check-runs",
+            owner,
+            repo,
         )
+        # A check-run from GitHub Actions only summarises the workflow run that
+        # produced it, so fetch those runs for the sha — but only when such a
+        # run exists: no actions check-run, no second ``gh`` call at all.
+        if any(
+            isinstance(run.get("app"), dict)
+            and run["app"].get("slug") == "github-actions"
+            for run in collected
+        ):
+            workflows = self._page_all(
+                "repos/{}/{}/actions/runs".format(quoted[0], quoted[1]),
+                f"head_sha={quoted[2]}&",
+                "workflow_runs",
+                "workflow-runs",
+                owner,
+                repo,
+            )
+            # First run wins per suite: the API lists newest first, so retries
+            # collapse onto the latest attempt. Only a positive int suite id on
+            # either side matches (bool excluded), and a matched run adds a
+            # 'workflow' key only when its id, workflow_id and str event are
+            # all usable — a suite with no usable run simply gets no key.
+            by_suite: dict = {}
+            for w in workflows:
+                suite_id = _pos_int(w.get("check_suite_id"))
+                if suite_id is None:
+                    continue
+                by_suite.setdefault(suite_id, w)
+            for run in collected:
+                suite = run.get("check_suite")
+                suite_id = _pos_int(suite.get("id")) if isinstance(suite, dict) else None
+                if suite_id is None:
+                    continue
+                wf = by_suite.get(suite_id)
+                if wf is None:
+                    continue
+                run_id = _pos_int(wf.get("id"))
+                wf_id = _pos_int(wf.get("workflow_id"))
+                event = wf.get("event")
+                if run_id is None or wf_id is None or not isinstance(event, str):
+                    continue
+                run["workflow"] = {
+                    "event": event,
+                    "workflow_id": wf_id,
+                    "name": wf.get("name"),
+                    "run_id": run_id,
+                }
+        return collected
+
+    # -- helpers ------------------------------------------------------------
+    def _page_all(
+        self, base: str, query: str, key: str, label: str, owner: str, repo: str
+    ) -> list[dict]:
+        """Collect every item under *key* across pages, refusing truncation.
+
+        Shared by the check-runs and the workflow-runs lookups so both carry
+        the same contract: at most 10 pages of 100, a non-zero ``gh`` exit
+        raises, and a ``total_count`` larger than what was collected fails
+        closed instead of returning a partial list. *base* is the API path
+        without a query; *query* is any prefix already opened there (empty for
+        check-runs, the ``head_sha`` scoping for workflow runs); *label* names
+        the list in the truncation error.
+        """
         collected: list[dict] = []
         total: int | None = None
         for page in range(1, 11):
-            path = f"{base}?per_page=100&page={page}"
+            path = f"{base}?{query}per_page=100&page={page}"
             proc = subprocess.run(
                 [self._gh, "api", path],
                 capture_output=True,
@@ -99,24 +188,23 @@ class CIAdapter:
             if proc.returncode != 0:
                 raise RuntimeError(f"gh api exited {proc.returncode} for {owner}/{repo}")
             data = json.loads(proc.stdout)
-            page_runs = data.get("check_runs") if isinstance(data, dict) else None
-            if not isinstance(page_runs, list):
-                page_runs = []
-            collected.extend([r for r in page_runs if isinstance(r, dict)])
+            page_items = data.get(key) if isinstance(data, dict) else None
+            if not isinstance(page_items, list):
+                page_items = []
+            collected.extend([r for r in page_items if isinstance(r, dict)])
             raw_total = data.get("total_count") if isinstance(data, dict) else None
             if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                 total = raw_total
                 if len(collected) >= total:
                     break
-            if len(page_runs) < 100:
+            if len(page_items) < 100:
                 break
         if total is not None and total > len(collected):
             raise RuntimeError(
-                f"check-runs truncated for {owner}/{repo}: expected {total}, got {len(collected)}"
+                f"{label} truncated for {owner}/{repo}: expected {total}, got {len(collected)}"
             )
         return collected
 
-    # -- helpers ------------------------------------------------------------
     @staticmethod
     def _validated_slug(slug: object) -> tuple[str, str]:
         if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
diff --git a/tests/test_evidence.py b/tests/test_evidence.py
index cf8162e4..b4a46b8d 100644
--- a/tests/test_evidence.py
+++ b/tests/test_evidence.py
@@ -8,6 +8,9 @@ are covered in ``test_evidence_adapters.py``; the other three verifiers in
 
 from __future__ import annotations
 
+import json
+from pathlib import Path
+
 import pytest
 
 from evidence_fakes import CLAIM, ITEM, OTHER_SHA, SHA, FakeCI, FakeGit, make_ctx, verify
@@ -411,3 +414,194 @@ def test_commit_ci_legacy_runs_without_suite_group_by_name():
 
 def test_ci_conclusion_all_non_dict_fails_closed():
     assert ev._ci_conclusion([None, 42, "x"], SHA) == f"no CI check-runs found for {SHA}"
+
+
+# --------------------------------------------------------------------------- #
+# commit+ci: workflow-dispatch runs grouped across suites
+# --------------------------------------------------------------------------- #
+
+def _wf_run(run_id, *, sid, rid, conclusion, status="completed",
+            name="pytest", event="workflow_dispatch", wf=7):
+    """A real-shaped check-run with the adapter-added ``workflow`` field."""
+    run = {"name": name, "id": rid, "head_sha": SHA, "status": status,
+           "conclusion": conclusion, "check_suite": {"id": sid},
+           "app": {"slug": "github-actions"},
+           "started_at": "2026-10-16T09:00:00Z",
+           "workflow": {"event": event, "workflow_id": wf, "name": "tests",
+                        "run_id": run_id}}
+    return run
+
+
+def test_ci_wf_cancelled_then_green_dispatch_different_suites_ok():
+    # One workflow dispatched twice: the first attempt was cancelled in suite A,
+    # the rerun went green in suite B -- the wf group must pick the rerun.
+    runs = [
+        _wf_run(9001, sid=11, rid=1, conclusion="cancelled"),
+        _wf_run(9002, sid=12, rid=2, conclusion="success"),
+    ]
+    assert ev._ci_conclusion(runs, SHA) is None
+
+
+def test_ci_wf_green_then_failed_dispatch_fails():
+    # The later dispatch failed even though its check-run id is LOWER than the
+    # green run's: inside a wf group the workflow run id decides, not the id.
+    runs = [
+        _wf_run(9001, sid=11, rid=500, conclusion="success"),
+        _wf_run(9002, sid=11, rid=400, conclusion="failure"),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "not green" in detail
+    assert "pytest=failure" in detail
+
+
+def test_ci_wf_pr_only_two_suites_stay_separate():
+    # No dispatch exists for this workflow, so PR runs keep the old
+    # (suite, app, name) keys: the failed suite still blocks today's way.
+    runs = [
+        _wf_run(8001, sid=21, rid=1, conclusion="failure", event="pull_request"),
+        _wf_run(8002, sid=22, rid=2, conclusion="success", event="pull_request"),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "not green" in detail
+    assert "failure" in detail
+    assert "pending" not in detail.lower()
+
+
+def test_ci_wf_pr_failure_then_later_green_dispatch_ok():
+    # The PR run's suite failed, but a later dispatch of the same workflow went
+    # green: the PR run joins the wf group and the green rerun wins.
+    runs = [
+        _wf_run(8001, sid=31, rid=10, conclusion="failure", event="pull_request"),
+        _wf_run(8002, sid=32, rid=11, conclusion="success"),
+    ]
+    assert ev._ci_conclusion(runs, SHA) is None
+
+
+def test_ci_wf_dispatch_failure_after_pr_green_fails():
+    # Same workflow, reversed verdicts: the later dispatch failure blocks even
+    # though the PR run (separate suite) was green.
+    runs = [
+        _wf_run(8001, sid=41, rid=30, conclusion="success", event="pull_request"),
+        _wf_run(8002, sid=42, rid=25, conclusion="failure"),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "not green" in detail
+    assert "pytest=failure" in detail
+
+
+def test_ci_wf_newer_in_progress_dispatch_after_green_is_pending():
+    # A dispatch that is still running outranks the older green one, so the
+    # group waits even though its check-run id is lower.
+    runs = [
+        _wf_run(9001, sid=51, rid=60, conclusion="success"),
+        _wf_run(9002, sid=51, rid=55, conclusion=None, status="in_progress"),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "pending" in detail.lower()
+
+
+def test_ci_wf_check_run_id_not_chronological_green_wins():
+    # Real data: a rerun gets check-run ids that can be LOWER than the older,
+    # failed run's ids. Ranking is (workflow.run_id, check-run id, started_at).
+    runs = [
+        _wf_run(9000, sid=61, rid=900, conclusion="failure"),
+        _wf_run(9001, sid=62, rid=7, conclusion="success"),
+    ]
+    assert ev._ci_conclusion(runs, SHA) is None
+
+
+def test_ci_wf_group_without_completed_run_is_pending():
+    runs = [_wf_run(9001, sid=71, rid=1, conclusion=None, status="in_progress")]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "pending" in detail.lower()
+
+
+def test_ci_wf_ambiguous_name_disambiguates_with_workflow_id():
+    runs = [
+        _wf_run(9001, sid=11, rid=1, conclusion="failure", wf=7),
+        _wf_run(9002, sid=12, rid=2, conclusion="failure", wf=99),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "pytest (workflow 7)=failure" in detail
+    assert "pytest (workflow 99)=failure" in detail
+
+
+def test_ci_wf_missing_workflow_id_stays_plain_suite_groups():
+    # A workflow dict with no usable workflow_id must behave exactly like a
+    # missing 'workflow' key: the suites stay separate plain groups, so the
+    # older failure still blocks although a newer green run sits next to it
+    # (a shared ("wf", 0, ...) group would let that green mask the failure).
+    failed = _wf_run(9001, sid=81, rid=1, conclusion="failure")
+    green = _wf_run(9002, sid=82, rid=2, conclusion="success", wf=None)
+    failed["workflow"].pop("workflow_id")
+    assert ev._ci_conclusion([failed, green], SHA) == f"CI is not green for {SHA} (pytest=failure)"
+    # Reversed verdicts split the same way: the newer failure in its own suite
+    # blocks on its own, never folded into the other suite's run.
+    older_green = _wf_run(9001, sid=83, rid=3, conclusion="success", wf=None)
+    newer_failed = _wf_run(9002, sid=84, rid=4, conclusion="failure", wf=None)
+    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"
+
+
+def test_ci_wf_bool_workflow_id_is_not_grouped():
+    # bool is not an id (same coercion as check-run ids): workflow_id=True must
+    # not forge a ("wf", 0, ...) group, so both suites stay plain groups and
+    # both failures are named instead of one hiding behind the other.
+    runs = [
+        _wf_run(9001, sid=91, rid=1, conclusion="failure", wf=True),
+        _wf_run(9002, sid=92, rid=2, conclusion="failure", wf=True),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail == (f"CI is not green for {SHA} "
+                      "(pytest (suite 91)=failure, pytest (suite 92)=failure)")
+
+
+def test_ci_wf_missing_run_id_stays_plain_suite_groups():
+    # A 'workflow' dict with no usable run_id counts as no 'workflow' at all,
+    # exactly like a missing workflow_id: without a positive run_id neither
+    # dispatch may join a wf group, so both keep their own plain
+    # (suite, app, name) key and the newer failure blocks instead of losing the
+    # rank race inside a shared ("wf", 7, ...) group, where the older green run
+    # wins on its higher check-run id (900 vs 7) and wrongly reports green.
+    older_green = _wf_run(9000, sid=95, rid=900, conclusion="success")
+    newer_failed = _wf_run(9001, sid=96, rid=7, conclusion="failure")
+    older_green["workflow"].pop("run_id")
+    newer_failed["workflow"].pop("run_id")
+    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"
+    # The same trap with only the newer failure lacking a run_id: its run with
+    # run_id 9000 still ranks the green run newest inside the merged group, so
+    # the failure must again sit in its own plain suite group and block.
+    older_green = _wf_run(9000, sid=97, rid=900, conclusion="success")
+    newer_failed = _wf_run(9001, sid=98, rid=7, conclusion="failure")
+    newer_failed["workflow"].pop("run_id")
+    assert ev._ci_conclusion([older_green, newer_failed], SHA) == f"CI is not green for {SHA} (pytest=failure)"
+
+
+def test_ci_conclusion_runs_without_workflow_key_unchanged():
+    runs = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert ev._ci_conclusion(runs, SHA) is None
+    flipped = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
+    ]
+    detail = ev._ci_conclusion(flipped, SHA)
+    assert detail is not None
+    assert "build=failure" in detail
+
+
+def test_ci_conclusion_replay_fixture_prererun_ok():
+    # Replay of the real 998f8f3 pre-rerun payload: a cancelled first attempt,
+    # a cancelled PR run and a dispatch whose pytest-postgres failed, all beaten
+    # by the newest dispatch run -- verdict must be green.
+    data = json.loads(
+        (Path(__file__).parent / "fixtures" / "ci_998f8f3_prererun.json").read_text()
+    )
+    assert ev._ci_conclusion(data["check_runs"], data["sha"]) is None
diff --git a/tests/test_evidence_adapters.py b/tests/test_evidence_adapters.py
index 285292f7..e71c4220 100644
--- a/tests/test_evidence_adapters.py
+++ b/tests/test_evidence_adapters.py
@@ -11,6 +11,8 @@ from pathlib import Path
 
 import pytest
 
+from plangraph.keeper import evidence_ci
+from plangraph.keeper.evidence_ci import CIAdapter
 from plangraph.keeper.evidence_decisions import DecisionsAdapter
 from plangraph.keeper.evidence_git import GitAdapter, parse_trailers
 from plangraph.keeper.evidence_runs import RunRecordAdapter
@@ -289,3 +291,223 @@ def test_semaphore_http_errors_distinct_and_token_free(monkeypatch, tmp_path, co
         SemaphoreAdapter().get(5, 1)
     assert str(excinfo.value) == msg
     assert "TOK" not in str(excinfo.value)
+
+
+# --------------------------------------------------------------------------- #
+# CIAdapter (gh api: check-runs, plus the workflow runs behind them)
+# --------------------------------------------------------------------------- #
+
+_SHA = "a" * 40
+
+
+def _fake_gh(monkeypatch, checks, *, runs=None, runs_rc=0):
+    """Serve ``gh api`` from fixtures: *checks* for the check-runs endpoint,
+    *runs* for ``actions/runs`` (one payload, or a list of per-page payloads);
+    *runs_rc* fails every workflow-runs call with that exit code.  ``runs=None``
+    means the workflow-runs endpoint must never be asked.  Returns the list of
+    requested API paths so a test can count and inspect the calls."""
+    paths: list[str] = []
+
+    def fake_run(cmd, **kwargs):
+        assert cmd[:2] == ["gh", "api"], cmd
+        path = cmd[2]
+        paths.append(path)
+        if "/actions/runs" in path:
+            if runs_rc:
+                return subprocess.CompletedProcess(cmd, runs_rc, "", "gh failed")
+            if runs is None:
+                raise AssertionError(f"workflow-runs endpoint must not be called: {path}")
+            page = int(path.rsplit("page=", 1)[1])
+            payload = runs if isinstance(runs, dict) else runs[page - 1]
+        else:
+            payload = checks
+        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
+
+    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
+    return paths
+
+
+def _check(app_slug, suite_id, *, name="build", check_id=1):
+    """One check-run as GitHub returns it, for the app under *app_slug*."""
+    return {
+        "id": check_id, "name": name, "status": "completed", "conclusion": "success",
+        "started_at": "2026-10-16T00:00:00Z", "head_sha": _SHA,
+        "app": {"slug": app_slug}, "check_suite": {"id": suite_id},
+        "details_url": f"https://example.invalid/{check_id}",
+    }
+
+
+def test_ci_adapter_backfills_workflow_for_dispatch_and_pr_suites(monkeypatch):
+    checks = {
+        "total_count": 3,
+        "check_runs": [
+            _check("github-actions", 111, name="build"),
+            _check("github-actions", 222, name="test", check_id=2),
+            _check("circleci", 333, name="lint", check_id=3),  # not an actions suite
+        ],
+    }
+    runs = {
+        "total_count": 2,
+        "workflow_runs": [
+            {"id": 9001, "check_suite_id": 111, "event": "push", "name": "CI",
+             "workflow_id": 5, "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+            {"id": 9002, "check_suite_id": 222, "event": "pull_request", "name": "Tests",
+             "workflow_id": 7, "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+        ],
+    }
+    paths = _fake_gh(monkeypatch, checks, runs=runs)
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert out[0]["workflow"] == {
+        "event": "push", "workflow_id": 5, "name": "CI", "run_id": 9001,
+    }
+    assert out[1]["workflow"] == {
+        "event": "pull_request", "workflow_id": 7, "name": "Tests", "run_id": 9002,
+    }
+    # The sha-scoped workflow-runs endpoint was used for the second call only.
+    assert len(paths) == 2
+    assert paths[1].startswith(
+        f"repos/owner/repo/actions/runs?head_sha={_SHA}&per_page=100&page=1"
+    )
+    # Existing keys are untouched alongside the added one.
+    assert out[0]["name"] == "build"
+    assert out[0]["check_suite"] == {"id": 111}
+
+
+def test_ci_adapter_unmatched_suite_gets_no_workflow_key(monkeypatch):
+    checks = {"total_count": 2, "check_runs": [
+        _check("github-actions", 777, name="build"),
+        _check("github-actions", 111, name="test", check_id=2),  # suite matches below
+    ]}
+    runs = {
+        "total_count": 1,
+        "workflow_runs": [
+            {"id": 42, "check_suite_id": 111, "event": "push", "name": "CI",
+             "workflow_id": 5, "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+        ],
+    }
+    _fake_gh(monkeypatch, checks, runs=runs)
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert len(out) == 2
+    assert "workflow" not in out[0]  # no workflow run shares suite 777
+    assert out[1]["workflow"] == {"event": "push", "workflow_id": 5,
+                                  "name": "CI", "run_id": 42}
+    assert out[0]["id"] == 1  # everything else stays as collected
+
+
+def test_ci_adapter_null_check_suite_ids_never_backfill(monkeypatch):
+    # A null check-suite id on either side may never match: the run keyed under
+    # None used to backfill the check-run whose own check_suite.id is None.
+    checks = {"total_count": 2, "check_runs": [
+        _check("github-actions", None, name="build"),
+        _check("github-actions", 111, name="test", check_id=2),
+    ]}
+    runs = {
+        "total_count": 2,
+        "workflow_runs": [
+            {"id": 9001, "check_suite_id": None, "event": "push", "name": "CI",
+             "workflow_id": 5, "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+            {"id": 9002, "check_suite_id": 111, "event": "push", "name": "CI",
+             "workflow_id": 5, "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+        ],
+    }
+    _fake_gh(monkeypatch, checks, runs=runs)
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert len(out) == 2
+    assert "workflow" not in out[0]  # no key is ever invented for a null suite
+    assert out[1]["workflow"] == {"event": "push", "workflow_id": 5,
+                                  "name": "CI", "run_id": 9002}
+
+
+def test_ci_adapter_bool_check_suite_id_never_backfills(monkeypatch):
+    # True == 1 in Python: without the int-only gate a bool suite id would
+    # hijack suite 1 and invent a 'workflow' key on either side of the match.
+    run = {"id": 42, "check_suite_id": True, "event": "push", "name": "CI",
+           "workflow_id": 5, "run_attempt": 1,
+           "status": "completed", "conclusion": "success"}
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", 1)]}
+    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert "workflow" not in out[0]  # a bool workflow-run suite id matches nothing
+
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", True)]}
+    run = dict(run, check_suite_id=1)
+    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert "workflow" not in out[0]  # a bool check-run suite id is no key either
+
+
+def test_ci_adapter_workflow_run_without_workflow_id_gets_no_key(monkeypatch):
+    # The suite matches, but the run has no workflow_id to group by: the
+    # check-run keeps its plain suite key instead of a half-empty 'workflow'.
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
+    runs = {
+        "total_count": 1,
+        "workflow_runs": [
+            {"id": 42, "check_suite_id": 111, "event": "workflow_dispatch",
+             "name": "CI", "run_attempt": 1,
+             "status": "completed", "conclusion": "success"},
+        ],
+    }
+    _fake_gh(monkeypatch, checks, runs=runs)
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert len(out) == 1
+    assert "workflow" not in out[0]
+
+
+@pytest.mark.parametrize("bad", [
+    {"id": None}, {"id": True}, {"id": 0}, {"id": "42"},
+    {"workflow_id": None}, {"workflow_id": True}, {"workflow_id": 0},
+    {"event": None}, {"event": 7},
+])
+def test_ci_adapter_incomplete_workflow_run_gets_no_key(monkeypatch, bad):
+    # 'workflow' is written only from a positive int id and workflow_id
+    # (bool excluded) and a str event; anything else adds no key at all.
+    run = {"id": 42, "check_suite_id": 111, "event": "push", "name": "CI",
+           "workflow_id": 5, "run_attempt": 1,
+           "status": "completed", "conclusion": "success"}
+    run.update(bad)
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
+    _fake_gh(monkeypatch, checks, runs={"total_count": 1, "workflow_runs": [run]})
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert "workflow" not in out[0]
+
+
+def test_ci_adapter_without_actions_runs_makes_exactly_one_gh_call(monkeypatch):
+    checks = {"total_count": 2, "check_runs": [
+        _check("circleci", 111, name="lint"),
+        _check("codecov", 222, name="coverage", check_id=2),
+    ]}
+    paths = _fake_gh(monkeypatch, checks)  # runs=None: second call must not happen
+    out = CIAdapter().check_runs("owner/repo", _SHA)
+    assert len(out) == 2
+    assert len(paths) == 1
+    assert "/check-runs" in paths[0]
+
+
+def test_ci_adapter_workflow_runs_truncated_is_refused(monkeypatch):
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
+    runs = [
+        {"total_count": 250,
+         "workflow_runs": [{"id": i, "check_suite_id": 111, "event": "push",
+                            "name": "CI", "workflow_id": 5, "run_attempt": 1,
+                            "status": "completed", "conclusion": "success"}
+                           for i in range(1, 101)]},
+        {"total_count": 250, "workflow_runs": []},
+    ]
+    paths = _fake_gh(monkeypatch, checks, runs=runs)
+    with pytest.raises(RuntimeError, match="workflow-runs truncated"):
+        CIAdapter().check_runs("owner/repo", _SHA)
+    assert any(p.endswith("page=2") and "/actions/runs" in p for p in paths)
+
+
+def test_ci_adapter_gh_failure_on_second_call_raises(monkeypatch):
+    checks = {"total_count": 1, "check_runs": [_check("github-actions", 111)]}
+    paths = _fake_gh(monkeypatch, checks, runs_rc=2)
+    with pytest.raises(RuntimeError, match="gh api exited 2"):
+        CIAdapter().check_runs("owner/repo", _SHA)
+    assert len(paths) == 2  # the failure came from the workflow-runs call


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
