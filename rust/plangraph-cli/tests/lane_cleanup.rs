//! `plangraph lane-cleanup` against a hooked fixture remote and a fake `gh`.
#![cfg(unix)]

mod lane_support;
use lane_support::{git, Fixture};
use std::fs;

fn lane_ref(name: &str, sha: &str) -> String {
    format!("refs/heads/lane/{name}:{sha}")
}

#[test]
fn lane_cleanup_deletes_finished_lanes_and_logs_shas() {
    let f = Fixture::new();
    let merged = f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    let patch = f.lane("lane/patch", "main", "p.txt", "p\n");
    f.main_pick(&patch);
    let r1 = f.lane("lane/sup-r1", "main", "s.txt", "s1\n");
    let r2 = f.lane("lane/sup-r2", "lane/sup-r1", "s2.txt", "s2\n");
    let live = f.lane("lane/live", "main", "l.txt", "l\n");
    assert!(f
        .record_replacement("B1", &lane_ref("sup-r1", &r1), &lane_ref("sup-r2", &r2))
        .status
        .success());
    f.hook_on(true);
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    assert_eq!(f.heads(), ["live", "sup-r2"], "{text}");
    let restore = f.restore_text();
    for sha in [&merged, &patch, &r1] {
        assert!(
            restore.contains(sha.as_str()),
            "restore row for {sha}\n{restore}"
        );
        assert!(text.contains(sha.as_str()), "log line for {sha}\n{text}");
        // The pinned object restores the branch.
        git(
            &f.work,
            &[
                "rev-parse",
                "--verify",
                &format!("refs/plangraph/restore/{sha}"),
            ],
        );
    }
    assert!(text.contains(&live), "{text}");
    assert!(
        text.contains("reconcile heads=5 eligible=3 protected=0 deleted=3 absent=0"),
        "{text}"
    );
}

#[test]
fn lane_cleanup_preserves_live_and_unproven_heads() {
    let f = Fixture::new();
    // An older round with unique work: the later round does not carry it.
    let uniq1 = f.lane("lane/uniq-r1", "main", "u1.txt", "u1\n");
    let uniq2 = f.lane("lane/uniq-r2", "main", "u2.txt", "u2\n");
    let rec = f.record_replacement(
        "B2",
        &lane_ref("uniq-r1", &uniq1),
        &lane_ref("uniq-r2", &uniq2),
    );
    assert!(
        !rec.status.success(),
        "replacement without the old work must be refused"
    );
    // A merge commit hides a resolution: the patch id of its parts proves nothing.
    f.lane("lane/mergey", "main", "x.txt", "x\n");
    git(&f.work, &["checkout", "-q", "-B", "side", "main"]);
    std::fs::write(f.work.join("y.txt"), "y\n").unwrap();
    git(&f.work, &["add", "."]);
    git(&f.work, &["commit", "-m", "side"]);
    git(&f.work, &["merge", "--no-ff", "-m", "merge", "lane/mergey"]);
    git(
        &f.work,
        &["push", "-q", "origin", "HEAD:refs/heads/lane/mergey2"],
    );
    // Whitespace changes behaviour: a patch-id match ("git cherry") is not proof, an exact merge is.
    let ws = f.lane("lane/ws", "main", "base.txt", "a b\n");
    git(&f.work, &["checkout", "-q", "main"]);
    std::fs::write(f.work.join("base.txt"), "ab\n").unwrap();
    git(
        &f.work,
        &["commit", "-qam", "same patch id, other whitespace"],
    );
    git(&f.work, &["push", "-q", "origin", "main"]);
    assert!(
        git(&f.work, &["cherry", "main", &ws]).starts_with("- "),
        "the fixture must be a patch-id twin"
    );
    // Merged by ancestry, but a claim, an open PR head and an open child PR base protect them.
    let claimed = f.lane("lane/claimed", "main", "c.txt", "c\n");
    let opened = f.lane("lane/opened", "main", "o.txt", "o\n");
    let parent = f.lane("lane/parent", "main", "pa.txt", "pa\n");
    git(&f.work, &["checkout", "-q", "main"]);
    for l in ["lane/claimed", "lane/opened", "lane/parent"] {
        git(&f.work, &["merge", "--no-edit", l]);
    }
    git(&f.work, &["push", "-q", "origin", "main"]);
    f.set("claims.json", r#"{"paths":[],"branches":["lane/claimed"]}"#);
    f.set_pulls(&[
        (5, "lane/opened", "main", &opened),
        (6, "lane/child", "lane/parent", &"1".repeat(40)),
    ]);
    // A moved head: the scope names the old SHA after the remote advanced.
    let moved_old = f.lane("lane/moving", "main", "mv.txt", "mv\n");
    f.main_ff("lane/moving");
    f.lane("lane/moving", "lane/moving", "mv2.txt", "mv2\n");
    f.hook_on(true);
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    for kept in [
        "uniq-r1", "uniq-r2", "mergey", "mergey2", "ws", "claimed", "opened", "parent", "moving",
    ] {
        assert!(
            f.heads().contains(&kept.to_string()),
            "{kept} deleted\n{text}"
        );
    }
    assert!(
        text.contains(&format!("lane/claimed {claimed}")) && text.contains("(claimed)"),
        "{text}"
    );
    assert!(
        text.contains("(open-pr-head)") && text.contains("(open-pr-base)"),
        "{text}"
    );
    let _ = parent;
    // The old SHA of the moved head is kept and reported; no restore row exists for it.
    let (_, scoped) = f.cleanup(&["--apply", "--refs", &lane_ref("moving", &moved_old)]);
    assert!(scoped.contains("absent-or-moved kept"), "{scoped}");
    assert!(f.heads().contains(&"moving".to_string()));
    assert!(!f.restore_text().contains(&moved_old));
}

#[test]
fn lane_cleanup_delete_needs_the_app_remote_and_unresolved_authority_keeps_everything() {
    let f = Fixture::new();
    let merged = f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    // The hook refuses a plain delete from any caller.
    f.hook_on(false);
    let plain = std::process::Command::new("git")
        .current_dir(&f.work)
        .args(["push", "origin", ":refs/heads/lane/merged"])
        .output()
        .unwrap();
    assert!(!plain.status.success());
    // The sweep through a remote that is not marked as the App's is refused, with a retry code.
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 3, "{text}");
    assert!(
        text.contains("refused") && f.heads() == ["merged"],
        "{text}"
    );
    // Unresolved claims keep every head, even with the App remote.
    f.hook_on(true);
    let out = f.cli(&[
        "lane-cleanup",
        "--repo",
        f.work.to_str().unwrap(),
        "--state",
        f.state.to_str().unwrap(),
        "--gh-repo",
        "fixture/origin",
        "--gh-host",
        "local",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(text.contains("protection unresolved"), "{text}");
    assert_eq!(f.heads(), ["merged"]);
    // The retry with resolved authority deletes it.
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    assert!(f.heads().is_empty() && text.contains(&merged), "{text}");
}

#[test]
fn probe_cleanup_closes_only_bound_terminal_runs() {
    let f = Fixture::new();
    let state = f.state.to_str().unwrap();
    let head = |c: &str| c.repeat(40);
    for (pr, run, c) in [(1, 11, "a"), (2, 12, "b"), (3, 13, "c")] {
        let out = f.cli(&[
            "lane-cleanup",
            "record-probe",
            "--state",
            state,
            "--gh-repo",
            "fixture/origin",
            "--pr",
            &pr.to_string(),
            "--head",
            &head(c),
            "--run-id",
            &run.to_string(),
        ]);
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let pr = |h: &str| format!(r#"{{"state":"OPEN","headRefOid":"{h}"}}"#);
    let run = |status: &str, h: &str| format!(r#"{{"status":"{status}","headSha":"{h}"}}"#);
    f.set("pr-1.json", &pr(&head("a")));
    f.set("run-11.json", &run("completed", &head("a")));
    f.set("pr-2.json", &pr(&head("b")));
    f.set("run-12.json", &run("in_progress", &head("b")));
    f.set("pr-3.json", &pr(&head("d")));
    f.set("run-13.json", &run("completed", &head("c")));
    f.set("pr-4.json", &pr(&head("e")));
    let out = f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    assert!(out.status.success(), "{text}");
    assert_eq!(
        fs::read_to_string(f.gh.join("closed.log")).unwrap(),
        "1\n",
        "{text}"
    );
    assert!(
        text.contains("#2") && text.contains("run-active") && text.contains("head-moved"),
        "{text}"
    );
    // A second run closes nothing again.
    f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    assert_eq!(fs::read_to_string(f.gh.join("closed.log")).unwrap(), "1\n");
}

#[test]
fn lane_cleanup_defers_history_classes_while_active_runs_are_unbound_and_binds_claim_paths() {
    let f = Fixture::new();
    let merged = f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    // In main with different history: only the exact in-main proof applies, not ancestry.
    let twin = f.lane("lane/twin", "main", "t.txt", "t\n");
    f.main_pick(&twin);
    // A worktree of this host owns a claim path: its branch stays.
    let owned = f.lane("lane/owned", "main", "o.txt", "o\n");
    f.main_ff("lane/owned");
    let tree = f.root.path().join("owned-tree");
    git(
        &f.work,
        &[
            "worktree",
            "add",
            "-q",
            tree.to_str().unwrap(),
            "lane/owned",
        ],
    );
    f.hook_on(true);
    f.set(
        "claims.json",
        &format!(
            r#"{{"paths":["{}"],"branches":[],"unbound_runs":2}}"#,
            tree.display()
        ),
    );
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    // Merged goes; the in-main twin waits for its runs to be bound; the claimed tree stays.
    assert_eq!(f.heads(), ["owned", "twin"], "{text}");
    assert!(
        text.contains("2 active runs have no bound publish ref"),
        "{text}"
    );
    assert!(
        text.contains(&merged) && text.contains(&owned) && text.contains(&twin),
        "{text}"
    );
    // A deleted merged head is no loss: the writer's next push recreates the branch.
    git(
        &f.work,
        &[
            "push",
            "-q",
            "origin",
            &format!("{merged}:refs/heads/lane/merged"),
        ],
    );
    assert!(f.heads().contains(&"merged".to_string()));
    f.set("claims.json", r#"{"paths":[],"branches":[]}"#);
    git(
        &f.work,
        &["worktree", "remove", "--force", tree.to_str().unwrap()],
    );
    git(&f.work, &["checkout", "-q", "--detach", "main"]);
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 0, "{text}");
    assert!(f.heads().is_empty(), "{text}");
}

#[test]
fn lane_cleanup_refuses_a_push_destination_or_host_other_than_the_checked_repository() {
    let f = Fixture::new();
    f.lane("lane/merged", "main", "m.txt", "m\n");
    f.main_ff("lane/merged");
    f.hook_on(true);
    // The fetch URL names the repository, the effective push URL names another one.
    git(
        &f.work,
        &["config", "remote.origin.pushurl", "/nowhere/else/other.git"],
    );
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 2, "{text}");
    assert!(
        text.contains("fetch and push URLs of origin name different repositories"),
        "{text}"
    );
    assert_eq!(f.heads(), ["merged"]);
    git(&f.work, &["config", "--unset", "remote.origin.pushurl"]);
    // A named push can reach several destinations: a valid first one does not hide a second.
    let own = git(&f.work, &["remote", "get-url", "origin"]);
    git(
        &f.work,
        &["config", "--add", "remote.origin.pushurl", own.trim()],
    );
    git(
        &f.work,
        &[
            "config",
            "--add",
            "remote.origin.pushurl",
            "/nowhere/else/other.git",
        ],
    );
    let (code, text) = f.cleanup(&["--apply"]);
    assert_eq!(code, 2, "{text}");
    assert_eq!(f.heads(), ["merged"]);
    git(&f.work, &["config", "--unset-all", "remote.origin.pushurl"]);
    // The same owner/name on another host is another repository.
    let out = f.cli(&[
        "lane-cleanup",
        "--repo",
        f.work.to_str().unwrap(),
        "--state",
        f.state.to_str().unwrap(),
        "--gh-repo",
        "fixture/origin",
        "--claims-reader",
        "echo '{}'",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stderr);
    assert_eq!(out.status.code(), Some(2), "{text}");
    assert!(
        text.contains("URLs of origin are not github.com/fixture/origin"),
        "{text}"
    );
    assert_eq!(f.heads(), ["merged"]);
}

#[test]
fn probe_cleanup_keeps_a_pr_whose_head_changes_between_the_two_reads() {
    let f = Fixture::new();
    let state = f.state.to_str().unwrap();
    let head = |c: &str| c.repeat(40);
    let out = f.cli(&[
        "lane-cleanup",
        "record-probe",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--pr",
        "7",
        "--head",
        &head("a"),
        "--run-id",
        "17",
    ]);
    assert!(out.status.success());
    f.set(
        "pr-7.json",
        &format!(r#"{{"state":"OPEN","headRefOid":"{}"}}"#, head("a")),
    );
    f.set(
        "pr-7.then.json",
        &format!(r#"{{"state":"OPEN","headRefOid":"{}"}}"#, head("b")),
    );
    f.set(
        "run-17.json",
        &format!(r#"{{"status":"completed","headSha":"{}"}}"#, head("a")),
    );
    let out = f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("head-moved"),
        "{text}"
    );
    assert!(!f.gh.join("closed.log").exists(), "{text}");
}

#[test]
fn probe_cleanup_keeps_a_pr_whose_head_moves_during_the_final_actions_lookup() {
    let f = Fixture::new();
    let state = f.state.to_str().unwrap();
    let head = |c: &str| c.repeat(40);
    let out = f.cli(&[
        "lane-cleanup",
        "record-probe",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--pr",
        "8",
        "--head",
        &head("a"),
        "--run-id",
        "18",
    ]);
    assert!(out.status.success());
    let pr = |h: &str| format!(r#"{{"state":"OPEN","headRefOid":"{}"}}"#, head(h));
    f.set("pr-8.json", &pr("a"));
    f.set("pr-8.moved.json", &pr("b"));
    f.set(
        "run-18.json",
        &format!(r#"{{"status":"completed","headSha":"{}"}}"#, head("a")),
    );
    // The second Actions lookup (the one right before the close) is when the push lands.
    f.set("on-run-18.second", "pr-8.moved.json pr-8.json");
    let out = f.cli(&[
        "lane-cleanup",
        "close-probes",
        "--state",
        state,
        "--gh-repo",
        "fixture/origin",
        "--apply",
    ]);
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("head-moved"),
        "{text}"
    );
    assert!(!f.gh.join("closed.log").exists(), "{text}");
}
