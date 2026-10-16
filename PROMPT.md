You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Review task: CommitCiVerifier (keeper evidence) must judge GitHub check-runs per check NAME, newest run only (highest id, tie-break started_at). Newest success/skipped/neutral = ok; failure/cancelled/timed_out/action_required/stale/other = fail naming the check; newest not completed = fail "CI pending ... retry later"; all counted runs skipped = fail closed; no runs = fail (unchanged); sha mismatch fail (unchanged). Real case a1b1da808: "Windows suite" skipped by design twice, shard b success then a newer in_progress run -> must be pending; a newer success -> ok. Raw check-run dicts: id, name, status, conclusion, started_at, head_sha; legacy fakes may omit status/id.

Vectors:
V1 Correct newest-per-name selection (id, tie-break started_at, non-int id, duplicates, missing name).
V2 Pending/failure/skipped precedence and fail-closed edge cases (status completed but conclusion null; status in_progress with stale conclusion; conclusion case/whitespace; unknown conclusion).
V3 Can any shape make a red or still-running CI report ok (false green)?
V4 Tests: do they cover each case, incl. the a1b1da808 shape, hermetically and non-vacuously?

Diff (git diff origin/main..HEAD):
diff --git a/src/plangraph/keeper/evidence.py b/src/plangraph/keeper/evidence.py
index 0e4859a..18be89e 100644
--- a/src/plangraph/keeper/evidence.py
+++ b/src/plangraph/keeper/evidence.py
@@ -119,6 +119,63 @@ def _commit_times(git: Any, repo: str, commit: str) -> tuple[float | None, float
 # commit+ci
 # --------------------------------------------------------------------------- #
 
+def _ci_conclusion(runs: Any, commit: str) -> str | None:
+    """Newest-run-per-name CI verdict: None means ok, else the fail detail. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
+    newest: dict[str, tuple[tuple[int, str], dict]] = {}
+    for run in runs:
+        if not isinstance(run, dict):
+            continue
+        name = str(run.get("name", "?"))
+        rid = run.get("id")
+        if isinstance(rid, bool):
+            rid_int = 0
+        elif isinstance(rid, int):
+            rid_int = rid
+        elif isinstance(rid, str) and rid.isdigit():
+            rid_int = int(rid)
+        else:
+            rid_int = 0
+        started = run.get("started_at")
+        started_str = str(started) if started is not None else ""
+        key = (rid_int, started_str)
+        prev = newest.get(name)
+        if prev is None or key > prev[0]:
+            newest[name] = (key, run)
+    if not newest:
+        return None
+    pending: list[str] = []
+    failures: list[tuple[str, str]] = []
+    seen: list[str] = []
+    for name in sorted(newest):
+        run = newest[name][1]
+        conc_raw = run.get("conclusion")
+        if conc_raw is None or (
+            isinstance(conc_raw, str) and conc_raw.strip() == ""
+        ):
+            pending.append(name)
+            continue
+        conc = str(conc_raw).strip()
+        if conc == "":
+            pending.append(name)
+            continue
+        if "status" in run and str(run.get("status")).strip().lower() != "completed":
+            pending.append(name)
+            continue
+        norm = conc.lower()
+        seen.append(norm)
+        if norm in ("success", "skipped", "neutral"):
+            continue
+        failures.append((name, str(conc_raw)))
+    if failures:
+        inner = ", ".join(f"{n}={c}" for n, c in failures)
+        return f"CI is not green for {commit} ({inner})"
+    if pending:
+        return f"CI pending for {commit} ({', '.join(pending)}), retry later"
+    if seen and all(c == "skipped" for c in seen):
+        return f"CI only skipped for {commit}"
+    return None
+
+
 class CommitCiVerifier:
     """A commit on ``main`` made for this item, dated after the claim, CI green."""
 
@@ -202,10 +259,9 @@ class CommitCiVerifier:
             return _fail(f"no CI check-runs found for {commit}", ref)
         if any(str(run.get("head_sha", "")) != commit for run in runs):
             return _fail(f"CI check-runs are for another sha than {commit}", ref)
-        not_green = [run for run in runs if str(run.get("conclusion", "")).lower() != "success"]
-        if not_green:
-            names = ", ".join(str(run.get("name", "?")) for run in not_green[:3])
-            return _fail(f"CI is not green for {commit} ({names})", ref)
+        detail = _ci_conclusion(runs, commit)
+        if detail is not None:
+            return _fail(detail, ref)
         return Result(True, f"commit {commit} on main with Plan-Item and green CI", ref)
 
 
diff --git a/tests/test_evidence.py b/tests/test_evidence.py
index f758f45..abb55a3 100644
--- a/tests/test_evidence.py
+++ b/tests/test_evidence.py
@@ -175,3 +175,175 @@ def test_commit_ci_lookup_error_is_sanitised():
     res = verify("commit+ci", {"commit": SHA}, ctx)
     assert res.ok is False
     assert "SECRET-TOKEN" not in res.detail
+
+
+# --------------------------------------------------------------------------- #
+# commit+ci: newest run per check name
+# --------------------------------------------------------------------------- #
+
+def test_commit_ci_newest_per_name_old_failure_newer_success_ok():
+    runs = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_newest_per_name_old_success_newer_failure_fails():
+    runs = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "build=failure" in res.detail
+
+
+def test_commit_ci_newest_tie_break_started_at():
+    runs = [
+        {"name": "build", "id": 5, "started_at": "2026-01-01T00:00:01Z",
+         "head_sha": SHA, "conclusion": "failure"},
+        {"name": "build", "id": 5, "started_at": "2026-01-01T00:00:02Z",
+         "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_newest_numeric_string_id_ranks_numerically():
+    runs = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
+        {"name": "build", "id": "99", "head_sha": SHA, "conclusion": "failure"},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "build=failure" in res.detail
+
+
+def test_commit_ci_newest_non_int_id_counts_as_zero():
+    runs = [
+        {"name": "build", "id": "abc", "started_at": "", "head_sha": SHA,
+         "conclusion": "failure"},
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+    tie_runs = [
+        {"name": "build", "id": "abc", "started_at": "2026-01-01T00:00:01Z",
+         "head_sha": SHA, "conclusion": "failure"},
+        {"name": "build", "id": None, "started_at": "2026-01-01T00:00:02Z",
+         "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=tie_runs))).ok is True
+
+
+def test_commit_ci_stale_success_with_in_progress_status_is_pending():
+    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "in_progress",
+             "conclusion": "success"}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "pending" in res.detail.lower()
+
+
+def test_commit_ci_missing_name_groups_under_unknown():
+    runs = [{"id": 1, "head_sha": SHA, "conclusion": "failure"}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "?=failure" in res.detail
+
+
+def test_commit_ci_completed_none_conclusion_is_pending():
+    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "completed",
+             "conclusion": None}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "pending" in res.detail.lower()
+
+
+def test_commit_ci_conclusion_whitespace_and_case_success_ok():
+    runs = [{"name": "build", "id": 1, "head_sha": SHA, "conclusion": " SUCCESS "}]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_conclusion_mixed_case_failure_fails():
+    runs = [{"name": "build", "id": 1, "head_sha": SHA, "conclusion": "Failure"}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "Failure" in res.detail
+
+
+def test_commit_ci_unknown_conclusion_weird_fails_naming_it():
+    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": "weird"}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "weird" in res.detail
+    assert "lint" in res.detail
+
+
+@pytest.mark.parametrize("conclusion", [
+    "failure", "cancelled", "timed_out", "action_required", "stale", "bogus",
+])
+def test_commit_ci_each_fail_conclusion_names_the_check(conclusion):
+    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": conclusion}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "lint" in res.detail
+    assert conclusion in res.detail
+    assert "green" in res.detail.lower()
+
+
+def test_commit_ci_skipped_plus_success_ok():
+    runs = [
+        {"name": "win", "id": 1, "head_sha": SHA, "conclusion": "skipped"},
+        {"name": "linux", "id": 2, "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_neutral_ok():
+    runs = [{"name": "lint", "id": 1, "head_sha": SHA, "conclusion": "neutral"}]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_all_skipped_fails_closed():
+    runs = [
+        {"name": "a", "id": 1, "head_sha": SHA, "conclusion": "skipped"},
+        {"name": "b", "id": 2, "head_sha": SHA, "conclusion": "skipped"},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "only skipped" in res.detail.lower()
+
+
+def test_commit_ci_pending_in_progress_fails_with_pending():
+    runs = [{"name": "build", "id": 1, "head_sha": SHA, "status": "in_progress",
+             "conclusion": None}]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "pending" in res.detail.lower()
+
+
+def test_commit_ci_a1b1da808_pending_then_ok():
+    pending_runs = [
+        {"name": "Windows suite", "id": 1, "head_sha": SHA,
+         "status": "completed", "conclusion": "skipped"},
+        {"name": "Windows suite", "id": 2, "head_sha": SHA,
+         "status": "completed", "conclusion": "skipped"},
+        {"name": "shard a", "id": 3, "head_sha": SHA,
+         "status": "completed", "conclusion": "success"},
+        {"name": "shard b", "id": 10, "head_sha": SHA,
+         "status": "completed", "conclusion": "success"},
+        {"name": "shard b", "id": 11, "head_sha": SHA,
+         "status": "in_progress", "conclusion": None},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=pending_runs)))
+    assert res.ok is False
+    assert "pending" in res.detail.lower()
+    ok_runs = pending_runs + [
+        {"name": "shard b", "id": 12, "head_sha": SHA,
+         "status": "completed", "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=ok_runs))).ok is True
+
+
+def test_commit_ci_no_runs_fails_closed_newest_logic():
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=[])))
+    assert res.ok is False


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
Your last two lines MUST be exactly 'TEST-GAMING: ...' then 'VERDICT: ...'
