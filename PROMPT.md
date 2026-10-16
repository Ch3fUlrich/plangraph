You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review a HIGH-risk change to a CI-evidence verifier (keeper commit+ci). Intent: for workflow_dispatch runs, the newest COMPLETED run per (workflow, job name) on a sha wins across check suites; an older cancelled run is ignored once a newer completed run exists; a newer failure/cancelled still blocks; PR-only suites unchanged; PR+dispatch with same (workflow,job) merge into one group. Adapter backfills run["workflow"] from GET actions/runs?head_sha= (join check_suite.id == check_suite_id). Real fields: check-run {id,name,status,conclusion,started_at,head_sha,app.slug,check_suite.id}; workflow_run {id,check_suite_id,event,name,workflow_id,run_attempt,status,conclusion}. Check-run ids are not chronological across reruns, so ranking is (workflow.run_id, check-run id, started_at).
Vectors (report per file:function, with a concrete failing input, or "no finding"):
V1 evidence.py:_ci_conclusion — false-green: a real failure masked (e.g. group-key collisions, wf_id 0/None, missing run_id ranks 0, completed-but-older failure hidden by a newer success that ran different work, skipped-only).
V2 evidence.py:_ci_conclusion — PR path regression: PR-only and non-workflow runs must behave exactly as before incl. message formats.
V3 evidence_ci.py:check_runs/_page_all — API field honesty: fake vs real gh contract, truncation/pagination refusal, suite->workflow join, unmatched suites, injection via head_sha query, second-call failure handling.


NOTE: THIRD pass; DELTA review of the newest commit (skipped/neutral fix) on head de6d7aa552230f0fd943840b6527754aab2d8905. Earlier findings (wf_id 0 collision, missing run_id ranking, check_suite_id None mis-join) were fixed; the 'skipped rerun beats older failure' case is intended (newest completed wins). Re-verify and look for anything new.

DELTA focus: in a wf group the newest completed non-skipped/neutral run wins; only if all completed runs are skipped/neutral does the newest win. Probe to verify: run 100 pytest=failure, run 200 pytest=skipped -> must be failure. Also look for new false-greens (e.g. neutral/skipped newer than a cancelled, pending interplay).
=== DIFF (delta) ===
diff --git a/src/plangraph/keeper/evidence.py b/src/plangraph/keeper/evidence.py
index a97dbc09..06491463 100644
--- a/src/plangraph/keeper/evidence.py
+++ b/src/plangraph/keeper/evidence.py
@@ -120,7 +120,7 @@ def _commit_times(git: Any, repo: str, commit: str) -> tuple[float | None, float
 # --------------------------------------------------------------------------- #
 
 def _ci_conclusion(runs: Any, commit: str) -> str | None:
-    """Newest-run-per-group CI verdict: None means ok, else the fail detail. Plain groups are (suite, app, name); a workflow_dispatch run forms a ("wf", workflow_id, app, name) group across suites that a matching PR run joins; a 'workflow' dict lacking a positive workflow_id or run_id counts as no 'workflow' at all. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
+    """Newest-run-per-group CI verdict: None means ok, else the fail detail. Plain groups are (suite, app, name); a workflow_dispatch run forms a ("wf", workflow_id, app, name) group across suites that a matching PR run joins. Inside a wf group the best run is the NEWEST COMPLETED run whose conclusion (stripped, lowercased) is not skipped/neutral, so a job-level `if:` skipping the newest run cannot mask an older real verdict; only when every completed run there is skipped/neutral does the newest of them win (so 'CI only skipped' still applies). A 'workflow' dict lacking a positive workflow_id or run_id counts as no 'workflow' at all. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
 
     def _id_int(value: Any) -> int:
         # Same coercion as check-run ids: bool is not an id, digit strings rank as ints.
@@ -218,18 +218,39 @@ def _ci_conclusion(runs: Any, commit: str) -> str | None:
             return False
         return not ("status" in run and str(run.get("status")).strip().lower() != "completed")
 
+    def _norm_conc(run: dict) -> str:
+        # Stripped, lowercased conclusion ('' when absent/blank).
+        conc_raw = run.get("conclusion")
+        return "" if conc_raw is None else str(conc_raw).strip().lower()
+
     pending_groups: list[tuple[str, Any, Any]] = []
     failure_groups: list[tuple[str, Any, Any, str]] = []
     seen: list[str] = []
     for gkey in sorted(groups, key=lambda k: (groups[k]["name"], str(groups[k]["sort"]), str(groups[k]["app"]))):
         g = groups[gkey]
         if g["wf"]:
-            # Only completed runs compete inside a wf group; a non-completed run
-            # ranked newer than the newest completed one (or none at all) is pending.
+            # Only completed runs compete inside a wf group, and among them the
+            # newest whose conclusion (stripped, lowercased) is NOT skipped or
+            # neutral wins: a job-level `if:` can skip the newest run while an
+            # older run holds the real verdict, which a plain newest-wins rule
+            # would report as a false green. Only when every completed run of
+            # the group is skipped/neutral is there no real verdict to prefer,
+            # so the newest of them wins and 'CI only skipped' still applies.
+            # A non-completed run ranked newer than the chosen best (or no
+            # completed run at all) keeps the group pending.
             best: tuple[Any, dict] | None = None
+            newest_completed: tuple[Any, dict] | None = None
             for rank, run in g["runs"]:
-                if _completed(run) and (best is None or rank > best[0]):
+                if not _completed(run):
+                    continue
+                if newest_completed is None or rank > newest_completed[0]:
+                    newest_completed = (rank, run)
+                if _norm_conc(run) in ("skipped", "neutral"):
+                    continue
+                if best is None or rank > best[0]:
                     best = (rank, run)
+            if best is None:
+                best = newest_completed
             if best is None or any(
                 rank > best[0] and not _completed(run) for rank, run in g["runs"]
             ):
diff --git a/tests/test_evidence.py b/tests/test_evidence.py
index b4a46b8d..eb89eab1 100644
--- a/tests/test_evidence.py
+++ b/tests/test_evidence.py
@@ -521,6 +521,54 @@ def test_ci_wf_group_without_completed_run_is_pending():
     assert "pending" in detail.lower()
 
 
+def test_ci_wf_newer_skipped_dispatch_does_not_mask_older_failure():
+    # Judge probe: workflow 7 dispatched twice, both completed, different
+    # suites, github-actions app. Run 100 ran pytest (job failed) and lint; the
+    # newer run 200 skipped pytest with a job-level `if:` and kept lint green.
+    # The skipped run must not win the wf group: the older real verdict still
+    # blocks, instead of the false green a newest-run-wins rule reported.
+    runs = [
+        _wf_run(100, sid=101, rid=11, conclusion="failure"),
+        _wf_run(100, sid=101, rid=12, conclusion="success", name="lint"),
+        _wf_run(200, sid=102, rid=21, conclusion="skipped"),
+        _wf_run(200, sid=102, rid=22, conclusion="success", name="lint"),
+    ]
+    detail = ev._ci_conclusion(runs, SHA)
+    assert detail is not None
+    assert "pytest" in detail
+    assert "failure" in detail
+    assert "pending" not in detail.lower()
+
+
+def test_ci_wf_group_all_completed_runs_skipped_is_only_skipped():
+    # When every completed run of a wf group is skipped/neutral there is no
+    # real verdict to prefer, so the newest of them wins and the standing
+    # "CI only skipped" rule applies unchanged.
+    runs = [
+        _wf_run(100, sid=111, rid=11, conclusion="skipped"),
+        _wf_run(200, sid=112, rid=21, conclusion="skipped"),
+    ]
+    assert ev._ci_conclusion(runs, SHA) == f"CI only skipped for {SHA}"
+
+
+def test_ci_wf_newer_neutral_dispatch_keeps_older_success_ok():
+    # A neutral verdict is not a real verdict either: the older success is the
+    # newest non-skipped/non-neutral run and still makes the group green.
+    runs = [
+        _wf_run(100, sid=121, rid=11, conclusion="success"),
+        _wf_run(200, sid=122, rid=21, conclusion="neutral"),
+    ]
+    assert ev._ci_conclusion(runs, SHA) is None
+    # ...and symmetrically a newer neutral never masks an older failure.
+    blocked = [
+        _wf_run(100, sid=123, rid=13, conclusion="failure"),
+        _wf_run(200, sid=124, rid=23, conclusion="neutral"),
+    ]
+    detail = ev._ci_conclusion(blocked, SHA)
+    assert detail is not None
+    assert "pytest=failure" in detail
+
+
 def test_ci_wf_ambiguous_name_disambiguates_with_workflow_id():
     runs = [
         _wf_run(9001, sid=11, rid=1, conclusion="failure", wf=7),


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
