    #[test]
    fn tick_reports_wall_clock_and_unit_jitter() {
        for _ in 0..32 {
            let t = tick_now();
            assert!(t.jitter >= 0.0 && t.jitter < 1.0, "jitter {}", t.jitter);
            assert!(t.unix_ms > 0);
        }
    }

    #[test]
    fn map_probe_names_every_outcome() {
        let branch = || "attempt/c1/1".to_string();
        assert_eq!(
            map_probe(Ok(ProbeOutcome::AlreadyDone), branch()).outcome,
            "already_done"
        );
        assert_eq!(
            map_probe(Ok(ProbeOutcome::Partial), branch()).outcome,
            "partial"
        );
        assert_eq!(
            map_probe(Ok(ProbeOutcome::Nothing), branch()).outcome,
            "nothing"
        );
        let err = ProbeError::Git(GitError::BadRev("bad".to_string()));
        let r = map_probe(Err(err), branch());
        assert_eq!(r.outcome, "error");
        assert_eq!(r.branch, "attempt/c1/1");
    }

    #[test]
    fn map_merge_names_every_outcome() {
        let merged = map_merge(
            MergeOutcome::Merged {
                sha: "abc".to_string(),
            },
            "abc".to_string(),
        );
        assert_eq!(merged.outcome, "merged");
        assert_eq!(merged.sha.as_deref(), Some("abc"));
        assert_eq!(merged.main_head, "abc");

        let moved = map_merge(MergeOutcome::BaseMoved, "def".to_string());
        assert_eq!((moved.outcome.as_str(), moved.sha), ("base_moved", None));
        assert_eq!(moved.main_head, "def");

        let conflict = map_merge(MergeOutcome::Conflict, "def".to_string());
        assert_eq!(
            (conflict.outcome.as_str(), conflict.sha),
            ("conflict", None)
        );
    }

    #[test]
    fn card_activities_construct_from_cloned_deps() {
        let d = deps();
        let a = CardActivities::new(d.clone());
        // The clone proves `Deps: Clone`; the field reads prove the wiring landed.
        assert_eq!(a.deps.cfg.check_tail_lines, d.cfg.check_tail_lines);
        assert_eq!(a.deps.cfg.heartbeat, d.cfg.heartbeat);
    }

    /// Local git runner, pinned identity and hermetic env like attempt.rs tests.
    fn git(dir: &Path, args: &[&str]) -> String {
        let out = Command::new("git")
            .args([
                "-c",
                "user.name=tester",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .current_dir(dir)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TERMINAL_PROMPT", "0")
            .stdin(Stdio::null())
            .output()
            .expect("git runs");
        assert!(
            out.status.success(),
            "git {args:?} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8_lossy(&out.stdout).trim().to_string()
    }

    fn write(dir: &Path, rel: &str, body: &str) {
        std::fs::write(dir.join(rel), body).unwrap();
    }

    fn commit_all(dir: &Path, msg: &str) -> String {
        git(dir, &["add", "-A"]);
        git(dir, &["commit", "-q", "-m", msg]);
        git(dir, &["rev-parse", "HEAD"])
    }

    fn repo() -> (TempDir, String) {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        git(p, &["init", "-q", "-b", "main"]);
        git(p, &["config", "commit.gpgsign", "false"]);
        write(p, "README.md", "hello\n");
        let sha = commit_all(p, "init");
        (dir, sha)
    }

    /// A branch one commit ahead of `base`, built in a scratch worktree so the
    /// caller's checkout stays on `main`.
    fn attempt_branch(p: &Path, name: &str, file: &str, body: &str, base: &str) -> String {
        let scratch = tempfile::tempdir().unwrap();
        let wt = scratch.path().join("wt");
        let wt_s = wt.to_string_lossy().into_owned();
        git(
            p,
            &[
                "worktree",
                "add",
                "-q",
                "-b",
                name,
                "--",
                wt_s.as_str(),
                base,
            ],
        );
        write(&wt, file, body);
        let head = commit_all(&wt, name);
        git(p, &["worktree", "remove", "--force", wt_s.as_str()]);
        head
    }

    fn probe_input(
        repo: &Path,
        workroot: &Path,
        n: u32,
        base: &str,
        check_cmd: &str,
    ) -> ProbeInput {
        ProbeInput {
            repo: repo.to_string_lossy().into_owned(),
            workroot: workroot.to_string_lossy().into_owned(),
            card: "c1".to_string(),
            n,
            base_sha: base.to_string(),
            check_cmd: check_cmd.to_string(),
        }
    }

    #[test]
    fn do_probe_sees_a_real_worktree() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        let wt = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();
        write(&wt.path, "done.txt", "x");
        commit_all(&wt.path, "work");

        let input = probe_input(r.path(), root.path(), 1, &base, "test -f done.txt");
        let res = do_probe(&tiny(), &input);
        assert_eq!(res.outcome, "already_done");
        assert_eq!(res.branch, AttemptId::new("c1", 1).branch());

        // A worktree with nothing beyond base is `nothing`, even if dirty.
        let id2 = AttemptId::new("c1", 2);
        let wt2 = worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 2).dir_name()),
            &id2,
        )
        .unwrap();
        write(&wt2.path, "wip.txt", "x");
        let input2 = probe_input(r.path(), root.path(), 2, &base, "true");
        assert_eq!(do_probe(&tiny(), &input2).outcome, "nothing");
    }

    #[test]
    fn do_probe_reports_error_for_a_bad_base() {
        let (r, base) = repo();
        let root = tempfile::tempdir().unwrap();
        let id = AttemptId::new("c1", 1);
        worktree::create(
            r.path(),
            &base,
            &root.path().join(AttemptId::new("c1", 1).dir_name()),
            &id,
        )
        .unwrap();

        let input = probe_input(r.path(), root.path(), 1, "deadbeef", "true");
        assert_eq!(do_probe(&tiny(), &input).outcome, "error");
    }

    fn merge_input(p: &Path, branch: &str, base: &str) -> MergeInput {
        MergeInput {
            repo: p.to_string_lossy().into_owned(),
            main_branch: "main".to_string(),
            branch: branch.to_string(),
            base_sha: base.to_string(),
            worktree: String::new(),
            check_cmd: None,
            confine: None,
        }
    }

    fn merge(keeper: &StubKeeper, input: &MergeInput) -> MergeResult {
        do_merge(keeper, &tiny(), input).unwrap()
    }

    /// An attempt worktree with its branch checked out, one commit ahead of
    /// `base`. Returns the worktree path (inside `root`).
    fn attempt_worktree(
        p: &Path,
        root: &Path,
        card: &str,
        file: &str,
        body: &str,
        base: &str,
    ) -> std::path::PathBuf {
        let id = AttemptId::new(card, 1);
        let wt =
            worktree::create(p, base, &root.join(AttemptId::new(card, 1).dir_name()), &id).unwrap();
        write(&wt.path, file, body);
        commit_all(&wt.path, "attempt");
        wt.path
    }

    #[test]
    fn do_merge_fast_forwards_main() {
        let (r, base) = repo();
        let p = r.path();
        let head = attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        let keeper = StubKeeper::default();
        let res = merge(&keeper, &merge_input(p, "attempt/c1/1", &base));
        assert_eq!(res.outcome, "merged");
        assert_eq!(res.sha, Some(head.clone()));
        assert_eq!(res.main_head, head);
        // The keeper lock was taken and given back.
        assert!(!keeper.merge_lock_held());
        assert_eq!(keeper.lock_calls().len(), 2);
    }

    #[test]
    fn do_merge_reports_a_moved_base() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        write(p, "y.txt", "y");
        let moved = commit_all(p, "advance");
        // No worktree handed over: nothing to rebase, so the move is reported.
        let res = merge(
            &StubKeeper::default(),
            &merge_input(p, "attempt/c1/1", &base),
        );
        assert_eq!(res.outcome, "base_moved");
        assert_eq!(res.sha, None);
        assert_eq!(res.main_head, moved);
    }

    #[test]
    fn do_merge_reports_a_conflict() {
        let (r, base) = repo();
        let p = r.path();
        // An unrelated root commit shares no ancestry with main, so `--ff-only`
        // refuses instead of producing a merge commit.
        git(p, &["checkout", "-q", "--orphan", "attempt/orphan/1"]);
        commit_all(p, "orphan");
        git(p, &["checkout", "-q", "main"]);
        let res = merge(
            &StubKeeper::default(),
            &merge_input(p, "attempt/orphan/1", &base),
        );
        assert_eq!(res.outcome, "conflict");
        assert_eq!(res.sha, None);
        assert_eq!(res.main_head, base);
    }

    #[test]
    fn do_merge_without_the_keeper_lock_is_retry_later() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        let keeper = StubKeeper::default();
        keeper.set_lock_unavailable(true);
        let res = merge(&keeper, &merge_input(p, "attempt/c1/1", &base));
        assert_eq!(res.outcome, "retry_later");
        // Nothing merged.
        assert_eq!(git(p, &["rev-parse", "main"]), base);
    }

    #[test]
    fn merge_owner_id_is_stable_in_process_and_carries_a_random_suffix() {
        let a = merge_owner_id();
        assert_eq!(a, merge_owner_id());
        let rest = a
            .strip_prefix(&format!("pg-worker-{}-", std::process::id()))
            .expect(a);
        assert_eq!(rest.len(), 32);
        assert!(rest.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(&rest[..16], &rest[16..], "two independent draws");
    }

    #[test]
    fn do_merge_refuses_a_stale_fencing_token() {
        let (r, base) = repo();
        let p = r.path();
        attempt_branch(p, "attempt/c1/1", "x.txt", "x", &base);
        attempt_branch(p, "attempt/c2/1", "z.txt", "z", &base);
        let keeper = StubKeeper::default();
        // A newer holder (token 2) merges first ...
        keeper.expire_merge_lock();
        let first = merge(&keeper, &merge_input(p, "attempt/c2/1", &base));
        assert_eq!(first.outcome, "merged");
        // ... then a holder whose keeper token is below the highest the repo
        // admitted is turned away (here the repo has seen a newer holder).
        let fence_file = git(p, &["rev-parse", "--git-common-dir"]);
        let fence_file = p.join(fence_file).join("pg-merge.fence");
        std::fs::write(&fence_file, "99").unwrap();
        let late = merge_input(p, "attempt/c1/1", &first.main_head);
        let res = merge(&keeper, &late);
        assert_eq!(res.outcome, "merge_refused");
        assert_eq!(git(p, &["rev-parse", "main"]), first.main_head);
        assert!(!keeper.merge_lock_held());
    }
