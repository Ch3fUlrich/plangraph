use super::build_tests::g;
use super::relane::*;
use super::relane_cli;
use std::path::PathBuf;

pub(super) struct Fx {
    pub(super) _dir: tempfile::TempDir,
    pub(super) origin: PathBuf,
    pub(super) work: PathBuf,
}

/// A bare origin and one clone; `commit_on` builds lane commits in a scratch clone and pushes them.
impl Fx {
    pub(super) fn new() -> Fx {
        let dir = tempfile::tempdir().unwrap();
        let origin = dir.path().join("origin.git");
        let work = dir.path().join("work");
        g(
            dir.path(),
            &["init", "-q", "--bare", "-b", "main", "origin.git"],
        );
        g(dir.path(), &["clone", "-q", "origin.git", "work"]);
        for f in ["a.txt", "b.txt", "c.txt"] {
            std::fs::write(work.join(f), "1\n2\n3\n").unwrap();
        }
        g(&work, &["add", "-A"]);
        g(&work, &["commit", "-q", "-m", "base"]);
        g(&work, &["push", "-q", "origin", "main"]);
        Fx {
            _dir: dir,
            origin,
            work,
        }
    }
    pub(super) fn scratch(&self, name: &str) -> PathBuf {
        let p = self._dir.path().join(name);
        g(self._dir.path(), &["clone", "-q", "origin.git", name]);
        p
    }
    /// A lane branch with one commit per (file, content), each with a Writer trailer.
    pub(super) fn lane(&self, branch: &str, writer: &str, edits: &[(&str, &str)]) -> String {
        let p = self.scratch(&format!("s-{}", branch.replace('/', "_")));
        g(&p, &["checkout", "-q", "-b", branch, "origin/main"]);
        for (f, c) in edits {
            std::fs::write(p.join(f), c).unwrap();
            g(&p, &["add", "-A"]);
            let msg = format!("edit {f}\n\nWriter: {writer}");
            g(&p, &["commit", "-q", "-m", &msg]);
        }
        g(&p, &["push", "-q", "origin", branch]);
        g(&p, &["rev-parse", "HEAD"])
    }
    /// A new main commit, from a scratch clone; returns the new tip.
    pub(super) fn advance(&self, file: &str, content: &str) -> String {
        let p = self.scratch(&format!("m-{file}"));
        std::fs::write(p.join(file), content).unwrap();
        g(&p, &["add", "-A"]);
        g(&p, &["commit", "-q", "-m", &format!("main {file}")]);
        g(&p, &["push", "-q", "origin", "main"]);
        g(&p, &["rev-parse", "HEAD"])
    }
    /// The lane as the registry records it: the head on the remote and the newest Writer trailer of its history.
    pub(super) fn input<'a>(&'a self, branch: &'a str, onto: &'a str) -> Lane<'a> {
        let head = g(&self.origin, &["rev-parse", branch]);
        let writer = g(
            &self.origin,
            &[
                "log",
                "-n",
                "20",
                "--no-merges",
                "--format=%(trailers:key=Writer,valueonly)",
                branch,
            ],
        )
        .lines()
        .find(|l| !l.is_empty())
        .unwrap_or("")
        .to_owned();
        Lane {
            repo: &self.work,
            remote: "origin",
            branch,
            onto,
            expected_head: Box::leak(head.into_boxed_str()),
            writer: Box::leak(writer.into_boxed_str()),
            parent_head: None,
            push: true,
        }
    }
    pub(super) fn log(&self, rev: &str, onto: &str) -> Vec<String> {
        g(
            &self.origin,
            &["log", "--format=%s", &format!("{onto}..{rev}")],
        )
        .lines()
        .map(str::to_owned)
        .collect()
    }
}

pub(super) fn rebased(o: Outcome) -> Report {
    match o {
        Outcome::Rebased(r) => r,
        other => panic!("not rebased: {}", other.line()),
    }
}

#[test]
fn two_lanes_relaned_in_parallel_in_one_clone_keep_only_their_own_commits() {
    let fx = Fx::new();
    fx.lane("lane/one-r1", "w-one", &[("a.txt", "one\n2\n3\n")]);
    fx.lane("lane/two-r4", "w-two", &[("b.txt", "1\ntwo\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let (a, b) = std::thread::scope(|s| {
        let a = s.spawn(|| relane(&fx.input("lane/one-r1", &onto)).unwrap());
        let b = s.spawn(|| relane(&fx.input("lane/two-r4", &onto)).unwrap());
        (a.join().unwrap(), b.join().unwrap())
    });
    let (a, b) = (rebased(a), rebased(b));
    assert_eq!(a.pushed.as_deref(), Some("lane/one-r2"));
    assert_eq!(b.pushed.as_deref(), Some("lane/two-r5"));
    assert_eq!(fx.log("lane/one-r2", &onto), ["edit a.txt"]);
    assert_eq!(fx.log("lane/two-r5", &onto), ["edit b.txt"]);
    assert!(a.patch_ids_equal && b.patch_ids_equal);
    assert_eq!((a.own_before, a.own_after), (1, 1));
    assert_ne!(a.old_head, a.new_head);
    // Neither the lane fetches nor the fetch of the new main touched FETCH_HEAD.
    assert!(!fx.work.join(".git/FETCH_HEAD").exists());
    // The remote lanes themselves were not moved: the new head is a new branch.
    assert_eq!(g(&fx.origin, &["rev-parse", "lane/one-r1"]), a.old_head);
}

#[test]
fn a_lane_whose_commits_are_all_upstream_is_refused_with_no_own_commit() {
    let fx = Fx::new();
    fx.lane("lane/dup-r1", "w-dup", &[("a.txt", "dup\n2\n3\n")]);
    // Main lands the same patch under another commit id.
    let onto = fx.advance("a.txt", "dup\n2\n3\n");
    match relane(&fx.input("lane/dup-r1", &onto)).unwrap() {
        Outcome::Refused { reason, .. } => assert!(reason.contains("no own commit"), "{reason}"),
        other => panic!("{}", other.line()),
    }
    let refs = g(&fx.origin, &["branch", "--list", "lane/dup-r2"]);
    assert!(refs.is_empty(), "nothing was pushed: {refs}");
}

#[test]
fn a_patch_already_on_main_is_dropped_and_the_rest_is_applied() {
    let fx = Fx::new();
    fx.lane(
        "lane/part-r1",
        "w-part",
        &[("a.txt", "dup\n2\n3\n"), ("b.txt", "1\nrest\n3\n")],
    );
    let onto = fx.advance("a.txt", "dup\n2\n3\n");
    let r = rebased(relane(&fx.input("lane/part-r1", &onto)).unwrap());
    assert_eq!(fx.log("lane/part-r2", &onto), ["edit b.txt"]);
    assert_eq!((r.own_before, r.own_after), (2, 1));
    assert!(
        !r.patch_ids_equal,
        "a dropped commit changes the patch list"
    );
}

#[test]
fn a_conflict_names_its_files_and_leaves_the_lane_untouched() {
    let fx = Fx::new();
    let old = fx.lane("lane/clash-r1", "w-clash", &[("a.txt", "lane\n2\n3\n")]);
    let onto = fx.advance("a.txt", "main\n2\n3\n");
    match relane(&fx.input("lane/clash-r1", &onto)).unwrap() {
        Outcome::Conflict { files, .. } => assert_eq!(files, ["a.txt"]),
        other => panic!("{}", other.line()),
    }
    assert_eq!(
        g(&fx.work, &["rev-parse", "refs/po/fetch/lane/clash-r1"]),
        old
    );
    assert!(g(&fx.origin, &["branch", "--list", "lane/clash-r2"]).is_empty());
    assert_eq!(
        g(&fx.work, &["worktree", "list", "--porcelain"])
            .matches("worktree ")
            .count(),
        1
    );
    assert!(g(&fx.work, &["status", "--porcelain"]).is_empty());
}

#[test]
fn a_fetched_head_that_is_not_the_recorded_head_is_refused() {
    let fx = Fx::new();
    fx.lane("lane/mix-r1", "w-mix", &[("a.txt", "mix\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let mut l = fx.input("lane/mix-r1", &onto);
    l.expected_head = "0000000000000000000000000000000000000001";
    match relane(&l).unwrap() {
        Outcome::Refused { reason, .. } => assert!(reason.contains("recorded head"), "{reason}"),
        other => panic!("{}", other.line()),
    }
}

#[test]
fn a_commit_of_another_writer_is_refused() {
    let fx = Fx::new();
    fx.lane("lane/foreign-r1", "w-other", &[("a.txt", "x\n2\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let mut l = fx.input("lane/foreign-r1", &onto);
    l.writer = "w-mine";
    match relane(&l).unwrap() {
        Outcome::Refused { reason, .. } => assert!(reason.contains("foreign Writer"), "{reason}"),
        other => panic!("{}", other.line()),
    }
}

#[test]
fn a_lane_that_already_contains_main_is_current() {
    let fx = Fx::new();
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let p = fx.scratch("cur");
    g(&p, &["checkout", "-q", "-b", "lane/cur-r1", "origin/main"]);
    std::fs::write(p.join("a.txt"), "cur\n2\n3\n").unwrap();
    g(&p, &["commit", "-q", "-am", "edit a.txt\n\nWriter: w-cur"]);
    g(&p, &["push", "-q", "origin", "lane/cur-r1"]);
    assert!(matches!(
        relane(&fx.input("lane/cur-r1", &onto)).unwrap(),
        Outcome::Current(_)
    ));
}

#[test]
fn the_cli_reports_every_lane_and_fails_when_one_was_refused() {
    let fx = Fx::new();
    fx.lane("lane/ok-r1", "w-ok", &[("b.txt", "1\nok\n3\n")]);
    fx.lane("lane/clash-r1", "w-clash", &[("a.txt", "lane\n2\n3\n")]);
    let onto = fx.advance("a.txt", "main\n2\n3\n");
    let spec = fx._dir.path().join("spec.json");
    let rec = |b: &str, w: &str| {
        format!(
            "{{\"branch\":\"{b}\",\"head\":\"{}\",\"writer\":\"{w}\"}}",
            g(&fx.origin, &["rev-parse", b])
        )
    };
    std::fs::write(
        &spec,
        format!(
            "[{},{}]",
            rec("lane/clash-r1", "w-clash"),
            rec("lane/ok-r1", "w-ok")
        ),
    )
    .unwrap();
    let mut flags = std::collections::BTreeMap::new();
    for (k, v) in [
        ("repo", fx.work.to_str().unwrap()),
        ("remote", "origin"),
        ("onto", onto.as_str()),
        ("spec", spec.to_str().unwrap()),
        ("push", "true"),
    ] {
        flags.insert(k.to_owned(), v.to_owned());
    }
    let err = relane_cli::run_cli(&flags).unwrap_err();
    assert!(err.contains("CONFLICT lane/clash-r1 a.txt"), "{err}");
    assert!(err.contains("REBASED lane/ok-r1"), "{err}");
    assert_eq!(fx.log("lane/ok-r2", &onto), ["edit b.txt"]);
}

#[test]
fn next_branch_counts_up() {
    assert_eq!(next_branch("lane/x-r4"), "lane/x-r5");
    assert_eq!(next_branch("lane/x"), "lane/x-r2");
    assert_eq!(next_branch("lane/x-rest"), "lane/x-rest-r2");
}

#[test]
fn remote_git_never_inherits_the_callers_repository_variables() {
    let fx = Fx::new();
    let cmd = super::relane_git::plain(&fx.work).unwrap();
    for var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"] {
        assert!(
            cmd.get_envs().any(|(k, v)| k == var && v.is_none()),
            "{var} is not removed"
        );
    }
}

/// A pre-push hook that notes when two pushes of the clone are inside it at once.
#[cfg(unix)]
#[test]
fn pushes_of_one_clone_never_overlap() {
    use std::os::unix::fs::PermissionsExt;
    let fx = Fx::new();
    fx.lane("lane/one-r1", "w-one", &[("a.txt", "one\n2\n3\n")]);
    fx.lane("lane/two-r1", "w-two", &[("b.txt", "1\ntwo\n3\n")]);
    let onto = fx.advance("c.txt", "1\n2\nmain\n");
    let marks = fx.work.join(".git/marks");
    std::fs::create_dir_all(&marks).unwrap();
    let hook = fx.work.join(".git/hooks/pre-push");
    std::fs::write(
        &hook,
        format!(
            "#!/bin/sh\nm='{}'\nmkdir \"$m/busy\" 2>/dev/null || touch \"$m/overlap\"\nsleep 0.4\nrmdir \"$m/busy\" 2>/dev/null\ntouch \"$m/ran-$$\"\n",
            marks.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();
    std::thread::scope(|s| {
        let a = s.spawn(|| relane(&fx.input("lane/one-r1", &onto)).unwrap());
        let b = s.spawn(|| relane(&fx.input("lane/two-r1", &onto)).unwrap());
        rebased(a.join().unwrap());
        rebased(b.join().unwrap());
    });
    let ran = std::fs::read_dir(&marks)
        .unwrap()
        .filter(|e| {
            e.as_ref()
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with("ran-")
        })
        .count();
    assert_eq!(ran, 2, "both pushes went through the hook");
    assert!(!marks.join("overlap").exists(), "two pushes ran at once");
}

#[test]
fn a_merge_of_main_inside_the_lane_is_not_an_own_commit() {
    let fx = Fx::new();
    let p = fx.scratch("withmerge");
    g(
        &p,
        &["checkout", "-q", "-b", "lane/merged-r1", "origin/main"],
    );
    std::fs::write(p.join("a.txt"), "lane\n2\n3\n").unwrap();
    g(&p, &["add", "-A"]);
    g(
        &p,
        &["commit", "-q", "-m", "edit a.txt\n\nWriter: w-merged"],
    );
    // main moves, and the lane merges it (the merge commit has no Writer trailer).
    fx.advance("c.txt", "1\n2\nfirst\n");
    g(&p, &["fetch", "-q", "origin"]);
    g(&p, &["merge", "-q", "--no-edit", "origin/main"]);
    g(&p, &["push", "-q", "origin", "lane/merged-r1"]);
    let onto = fx.advance("b.txt", "1\n2\nsecond\n");
    let l = fx.input("lane/merged-r1", &onto);
    let r = rebased(relane(&l).unwrap());
    assert_eq!((r.own_before, r.own_after), (1, 1));
    assert_eq!(fx.log("lane/merged-r2", &onto), ["edit a.txt"]);
}
