You have NO tools and no repository access. The full material is inline below. Answer in plain text; never emit tool calls.

Delta review. Context: CommitCiVerifier judges GitHub check-runs per check, newest run only; success/skipped/neutral ok; failure/cancelled/timed_out/action_required/stale/other fail naming the check; newest not completed = "CI pending ... retry later"; all skipped = fail closed; no runs = fail. A previous review round found: (1) newest-per-NAME across different check suites let a newer 'test' success in workflow B hide an older 'test' failure in workflow A. GitHub's default filter=latest already supersedes re-runs inside one suite. Fix applied: group key (check_suite.id, app.slug, name), newest within the group (id, tie started_at). (2) empty/non-dict input returned ok -> now fail closed. (3) CIAdapter read only page 1 (30 runs) -> now paginates per_page=100, cap 10 pages, raises if total_count > collected.
Real case a1b1da808: 'Windows suite' skipped by design in two separate workflow runs (suites), shard b success in suite 1 and in_progress in suite 2 -> pending; when suite 2 shard b succeeds -> ok.

Vectors:
V1 Can any run shape still produce a false green or hide a red/in-progress run (grouping key edge cases: missing check_suite/app, unhashable ids, same name in different apps)?
V2 Pagination correctness: termination, off-by-one, total_count semantics, cap, truncation raise, mocked-subprocess compatibility, path injection.
V3 Tests: fail-first cross-suite case, same-suite supersede, a1b1da808 shape, pagination, truncated, fail-closed; non-vacuous?

Diff (git diff 162138558a9d2c485f8608db0ba77be4a0bd55bd..HEAD):
diff --git a/src/plangraph/keeper/evidence.py b/src/plangraph/keeper/evidence.py
index 18be89e..36d4050 100644
--- a/src/plangraph/keeper/evidence.py
+++ b/src/plangraph/keeper/evidence.py
@@ -120,12 +120,32 @@ def _commit_times(git: Any, repo: str, commit: str) -> tuple[float | None, float
 # --------------------------------------------------------------------------- #
 
 def _ci_conclusion(runs: Any, commit: str) -> str | None:
-    """Newest-run-per-name CI verdict: None means ok, else the fail detail. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
-    newest: dict[str, tuple[tuple[int, str], dict]] = {}
+    """Newest-run-per-(suite, app, name) CI verdict: None means ok, else the fail detail. A run with no 'status' key and a non-empty conclusion counts as completed (legacy fakes)."""
+    newest: dict[tuple[Any, Any, str], tuple[tuple[int, str], dict]] = {}
     for run in runs:
         if not isinstance(run, dict):
             continue
         name = str(run.get("name", "?"))
+        suite: Any = None
+        cs = run.get("check_suite")
+        if isinstance(cs, dict) and "id" in cs:
+            sid = cs["id"]
+            try:
+                hash(sid)
+            except TypeError:
+                pass
+            else:
+                suite = sid
+        app: Any = None
+        ap = run.get("app")
+        if isinstance(ap, dict) and "slug" in ap:
+            slug = ap["slug"]
+            try:
+                hash(slug)
+            except TypeError:
+                pass
+            else:
+                app = slug
         rid = run.get("id")
         if isinstance(rid, bool):
             rid_int = 0
@@ -138,39 +158,71 @@ def _ci_conclusion(runs: Any, commit: str) -> str | None:
         started = run.get("started_at")
         started_str = str(started) if started is not None else ""
         key = (rid_int, started_str)
-        prev = newest.get(name)
+        gkey = (suite, app, name)
+        prev = newest.get(gkey)
         if prev is None or key > prev[0]:
-            newest[name] = (key, run)
+            newest[gkey] = (key, run)
     if not newest:
-        return None
-    pending: list[str] = []
-    failures: list[tuple[str, str]] = []
+        return f"no CI check-runs found for {commit}"
+    pending_groups: list[tuple[str, Any, Any]] = []
+    failure_groups: list[tuple[str, Any, Any, str]] = []
     seen: list[str] = []
-    for name in sorted(newest):
-        run = newest[name][1]
+    for gkey in sorted(newest, key=lambda k: (k[2], str(k[0]), str(k[1]))):
+        run = newest[gkey][1]
+        name = gkey[2]
+        suite, app = gkey[0], gkey[1]
         conc_raw = run.get("conclusion")
         if conc_raw is None or (
             isinstance(conc_raw, str) and conc_raw.strip() == ""
         ):
-            pending.append(name)
+            pending_groups.append((name, suite, app))
             continue
         conc = str(conc_raw).strip()
         if conc == "":
-            pending.append(name)
+            pending_groups.append((name, suite, app))
             continue
         if "status" in run and str(run.get("status")).strip().lower() != "completed":
-            pending.append(name)
+            pending_groups.append((name, suite, app))
             continue
         norm = conc.lower()
         seen.append(norm)
         if norm in ("success", "skipped", "neutral"):
             continue
-        failures.append((name, str(conc_raw)))
-    if failures:
-        inner = ", ".join(f"{n}={c}" for n, c in failures)
+        failure_groups.append((name, suite, app, str(conc_raw)))
+    if failure_groups:
+        from collections import Counter
+
+        counts = Counter(n for n, _, _, _ in failure_groups)
+
+        def _fail_disp(n: str, s: Any, a: Any) -> str:
+            if counts[n] <= 1:
+                return n
+            if s is not None:
+                return f"{n} (suite {s})"
+            if a is not None:
+                return f"{n} ({a})"
+            return n
+
+        inner = ", ".join(
+            f"{_fail_disp(n, s, a)}={c}" for n, s, a, c in failure_groups
+        )
         return f"CI is not green for {commit} ({inner})"
-    if pending:
-        return f"CI pending for {commit} ({', '.join(pending)}), retry later"
+    if pending_groups:
+        from collections import Counter
+
+        counts = Counter(n for n, _, _ in pending_groups)
+
+        def _pend_disp(n: str, s: Any, a: Any) -> str:
+            if counts[n] <= 1:
+                return n
+            if s is not None:
+                return f"{n} (suite {s})"
+            if a is not None:
+                return f"{n} ({a})"
+            return n
+
+        displays = [_pend_disp(n, s, a) for n, s, a in pending_groups]
+        return f"CI pending for {commit} ({', '.join(displays)}), retry later"
     if seen and all(c == "skipped" for c in seen):
         return f"CI only skipped for {commit}"
     return None
diff --git a/src/plangraph/keeper/evidence_ci.py b/src/plangraph/keeper/evidence_ci.py
index 0da0e88..8e34dc3 100644
--- a/src/plangraph/keeper/evidence_ci.py
+++ b/src/plangraph/keeper/evidence_ci.py
@@ -82,23 +82,39 @@ class CIAdapter:
         if not valid_commit_ref(sha):
             raise ValueError("check-runs requires a full lowercase commit sha")
         owner, repo = self._validated_slug(self._slug(repo_slug_or_path))
-        path = "repos/{}/{}/commits/{}/check-runs".format(
+        base = "repos/{}/{}/commits/{}/check-runs".format(
             quote(owner, safe=""), quote(repo, safe=""), quote(sha, safe="")
         )
-        proc = subprocess.run(
-            [self._gh, "api", path],
-            capture_output=True,
-            text=True,
-            timeout=self._timeout,
-            check=False,
-        )
-        if proc.returncode != 0:
-            raise RuntimeError(f"gh api exited {proc.returncode} for {owner}/{repo}")
-        data = json.loads(proc.stdout)
-        runs = data.get("check_runs") if isinstance(data, dict) else None
-        if not isinstance(runs, list):
-            return []
-        return [r for r in runs if isinstance(r, dict)]
+        collected: list[dict] = []
+        total: int | None = None
+        for page in range(1, 11):
+            path = f"{base}?per_page=100&page={page}"
+            proc = subprocess.run(
+                [self._gh, "api", path],
+                capture_output=True,
+                text=True,
+                timeout=self._timeout,
+                check=False,
+            )
+            if proc.returncode != 0:
+                raise RuntimeError(f"gh api exited {proc.returncode} for {owner}/{repo}")
+            data = json.loads(proc.stdout)
+            page_runs = data.get("check_runs") if isinstance(data, dict) else None
+            if not isinstance(page_runs, list):
+                page_runs = []
+            collected.extend([r for r in page_runs if isinstance(r, dict)])
+            raw_total = data.get("total_count") if isinstance(data, dict) else None
+            if isinstance(raw_total, int) and not isinstance(raw_total, bool):
+                total = raw_total
+                if len(collected) >= total:
+                    break
+            if len(page_runs) < 100:
+                break
+        if total is not None and total > len(collected):
+            raise RuntimeError(
+                f"check-runs truncated for {owner}/{repo}: expected {total}, got {len(collected)}"
+            )
+        return collected
 
     # -- helpers ------------------------------------------------------------
     @staticmethod
diff --git a/tests/test_evidence.py b/tests/test_evidence.py
index abb55a3..cf8162e 100644
--- a/tests/test_evidence.py
+++ b/tests/test_evidence.py
@@ -347,3 +347,67 @@ def test_commit_ci_a1b1da808_pending_then_ok():
 def test_commit_ci_no_runs_fails_closed_newest_logic():
     res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=[])))
     assert res.ok is False
+
+
+def test_commit_ci_same_name_different_suites_both_evaluated():
+    runs = [
+        {"name": "test", "id": 1, "head_sha": SHA,
+         "check_suite": {"id": 100}, "status": "completed", "conclusion": "failure"},
+        {"name": "test", "id": 2, "head_sha": SHA,
+         "check_suite": {"id": 200}, "status": "completed", "conclusion": "success"},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs)))
+    assert res.ok is False
+    assert "test" in res.detail
+
+
+def test_commit_ci_same_suite_rerun_supersedes():
+    runs = [
+        {"name": "test", "id": 1, "head_sha": SHA,
+         "check_suite": {"id": 100}, "status": "completed", "conclusion": "failure"},
+        {"name": "test", "id": 2, "head_sha": SHA,
+         "check_suite": {"id": 100}, "status": "completed", "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+
+
+def test_commit_ci_suite_grouped_a1b1da808_pending_then_ok():
+    pending_runs = [
+        {"name": "Windows suite", "id": 1, "head_sha": SHA,
+         "check_suite": {"id": 10}, "status": "completed", "conclusion": "skipped"},
+        {"name": "Windows suite", "id": 2, "head_sha": SHA,
+         "check_suite": {"id": 11}, "status": "completed", "conclusion": "skipped"},
+        {"name": "shard a", "id": 3, "head_sha": SHA,
+         "check_suite": {"id": 10}, "status": "completed", "conclusion": "success"},
+        {"name": "shard b", "id": 10, "head_sha": SHA,
+         "check_suite": {"id": 10}, "status": "completed", "conclusion": "success"},
+        {"name": "shard b", "id": 11, "head_sha": SHA,
+         "check_suite": {"id": 11}, "status": "in_progress", "conclusion": None},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=pending_runs)))
+    assert res.ok is False
+    assert "pending" in res.detail.lower()
+    ok_runs = pending_runs + [
+        {"name": "shard b", "id": 12, "head_sha": SHA,
+         "check_suite": {"id": 11}, "status": "completed", "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=ok_runs))).ok is True
+
+
+def test_commit_ci_legacy_runs_without_suite_group_by_name():
+    runs = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "failure"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "success"},
+    ]
+    assert verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=runs))).ok is True
+    flipped = [
+        {"name": "build", "id": 1, "head_sha": SHA, "conclusion": "success"},
+        {"name": "build", "id": 2, "head_sha": SHA, "conclusion": "failure"},
+    ]
+    res = verify("commit+ci", {"commit": SHA}, make_ctx(ci=FakeCI(runs=flipped)))
+    assert res.ok is False
+    assert "build=failure" in res.detail
+
+
+def test_ci_conclusion_all_non_dict_fails_closed():
+    assert ev._ci_conclusion([None, 42, "x"], SHA) == f"no CI check-runs found for {SHA}"
diff --git a/tests/test_evidence_hardening.py b/tests/test_evidence_hardening.py
index 3bb1a6e..fd23ad4 100644
--- a/tests/test_evidence_hardening.py
+++ b/tests/test_evidence_hardening.py
@@ -189,7 +189,10 @@ def test_ci_adapter_builds_path_from_validated_components(monkeypatch):
 
     monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
     CIAdapter().check_runs("My-Org/repo.name", SHA)
-    assert seen["cmd"][:3] == ["gh", "api", f"repos/My-Org/repo.name/commits/{SHA}/check-runs"]
+    assert seen["cmd"][:2] == ["gh", "api"]
+    assert seen["cmd"][2].startswith(f"repos/My-Org/repo.name/commits/{SHA}/check-runs")
+    assert "per_page=100" in seen["cmd"][2]
+    assert "page=1" in seen["cmd"][2]
 
 
 # --------------------------------------------------------------------------- #
@@ -366,3 +369,41 @@ def test_semaphore_opener_ignores_proxy_env(monkeypatch):
     default = urllib.request.build_opener()
     assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
                for h in default.handlers)  # the env really is read by default
+
+
+def test_ci_adapter_paginates_two_pages(monkeypatch):
+    calls: list = []
+
+    def fake_run(cmd, **kwargs):
+        calls.append(cmd)
+        path = cmd[2]
+        if path.endswith("page=1"):
+            runs = [{"id": i, "name": f"c{i}"} for i in range(1, 101)]
+            payload = {"check_runs": runs, "total_count": 105}
+        elif path.endswith("page=2"):
+            runs = [{"id": i, "name": f"c{i}"} for i in range(101, 106)]
+            payload = {"check_runs": runs, "total_count": 105}
+        else:
+            raise AssertionError(f"unexpected page in {path!r}")
+        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
+
+    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
+    runs = CIAdapter().check_runs("owner/repo", SHA)
+    assert len(runs) == 105
+    assert any("per_page=100" in c[2] and c[2].endswith("page=2") for c in calls)
+    assert any("per_page=100" in c[2] and c[2].endswith("page=1") for c in calls)
+
+
+def test_ci_adapter_truncated_raises(monkeypatch):
+    def fake_run(cmd, **kwargs):
+        path = cmd[2]
+        if path.endswith("page=1"):
+            runs = [{"id": i, "name": f"c{i}"} for i in range(1, 101)]
+            payload = {"check_runs": runs, "total_count": 250}
+        else:
+            payload = {"check_runs": [], "total_count": 250}
+        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
+
+    monkeypatch.setattr(evidence_ci.subprocess, "run", fake_run)
+    with pytest.raises(RuntimeError, match="check-runs truncated"):
+        CIAdapter().check_runs("owner/repo", SHA)


=== END OF MATERIAL. FINAL REMINDER ON ANSWER SHAPE
1. Answer each vector V1..Vn under its own heading with file:function evidence.
2. Then answer the standing question below as ONE plain line starting at column 0, no bold, no trailing period. For "no" the line MUST end with the literal text ": 0 hits" (the parser rejects any other ending), e.g.
TEST-GAMING: no - searched tests/ and src/ for test-only branches and env checks: 0 hits
3. The very last line is plain, no bold:
VERDICT: pass
(or VERDICT: fix - <findings by severity> / VERDICT: halt - <why>)
Your last two lines MUST be exactly 'TEST-GAMING: ...' then 'VERDICT: ...'
