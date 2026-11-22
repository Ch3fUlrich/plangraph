use super::build::*;
use super::git;
use super::train::*;
use std::path::Path;
use std::process::Command;

struct Fx {
    dir: tempfile::TempDir,
}

fn g(dir: &Path, args: &[&str]) -> String {
    let o = Command::new("git")
        .current_dir(dir)
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("HOME", "/nonexistent")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_AUTHOR_NAME", "t")
        .env("GIT_AUTHOR_EMAIL", "t@example.invalid")
        .env("GIT_COMMITTER_NAME", "t")
        .env("GIT_COMMITTER_EMAIL", "t@example.invalid")
        .args(args)
        .output()
        .unwrap();
    assert!(
        o.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&o.stderr)
    );
    String::from_utf8_lossy(&o.stdout).trim().to_owned()
}

impl Fx {
    fn new() -> Fx {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path();
        g(p, &["init", "-q", "-b", "main"]);
        std::fs::write(
            p.join("shared.txt"),
            (1..=6).map(|i| format!("line {i}\n")).collect::<String>(),
        )
        .unwrap();
        std::fs::write(p.join("log.txt"), "entry 0\n").unwrap();
        g(p, &["add", "-A"]);
        g(p, &["commit", "-q", "-m", "base"]);
        Fx { dir }
    }
    fn path(&self) -> &Path {
        self.dir.path()
    }
    fn base(&self) -> String {
        g(self.path(), &["rev-parse", "main"])
    }
    /// One commit on a new branch from `from`: replaces `file` with `content`; returns the new head.
    fn commit(&self, from: &str, file: &str, content: &str) -> String {
        let p = self.path();
        g(p, &["checkout", "-q", "--detach", from]);
        std::fs::write(p.join(file), content).unwrap();
        g(p, &["add", "-A"]);
        g(p, &["commit", "-q", "-m", &format!("edit {file}")]);
        let head = g(p, &["rev-parse", "HEAD"]);
        g(p, &["checkout", "-q", "main"]);
        head
    }
    fn input(&self, parts: Vec<PartIn>, attributes: &str, cap: Option<usize>) -> BuildInput<'_> {
        BuildInput {
            repo: self.path(),
            build: "b1".into(),
            base: self.base(),
            parts,
            attributes: attributes.into(),
            cap,
            resolutions: vec![],
            protected: vec![],
        }
    }
}

fn part(pr: u64, head: &str, parent: Option<(u64, &str)>) -> PartIn {
    PartIn {
        pr,
        head: head.into(),
        parent: parent.map(|(pr, head)| Parent {
            pr,
            head: head.into(),
        }),
        receipt: PartReceipt {
            run_id: format!("run-{pr}"),
            head: head.into(),
        },
    }
}

fn lines(n: &[&str]) -> String {
    n.iter().map(|l| format!("{l}\n")).collect()
}

#[test]
fn a_train_of_three_fixture_parts_matches_its_manifest() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    let c = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&[
            "line 1",
            "line 2",
            "line 3",
            "line 4",
            "line 5",
            "changed 6",
        ]),
    );
    let parts = vec![part(1, &a, None), part(2, &b, None), part(3, &c, None)];
    let out = build(&fx.input(parts.clone(), "", None)).unwrap();
    let m = out.manifest.unwrap();
    assert_eq!(
        m.parts.iter().map(|p| p.pr).collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    assert_eq!(m.merges.len(), 3);
    assert_eq!(m.base, fx.base());
    assert!(m.left_out.is_empty());
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest(fx.path(), &m, &parts).unwrap();
    verify_result(fx.path(), &m.base, &out.result, Some(&m)).unwrap();
    // Each merge has exactly its two expected parents.
    let first = g(
        fx.path(),
        &["rev-list", "--parents", "-n", "1", &m.merges[1]],
    );
    assert_eq!(
        first.split(' ').collect::<Vec<_>>(),
        vec![m.merges[1].as_str(), m.merges[0].as_str(), b.as_str()]
    );
}

#[test]
fn dropping_a_parent_drops_its_child_and_the_child_cannot_bring_the_parents_commits_back() {
    let fx = Fx::new();
    let x = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "X", "line 3", "line 4", "line 5", "line 6"]),
    );
    let p = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let c = fx.commit(&p, "child.txt", "child\n");
    let other = fx.commit(&fx.base(), "other.txt", "o\n");
    let parts = vec![
        part(1, &x, None),
        part(2, &p, None),
        part(3, &c, Some((2, &p))),
        part(4, &other, None),
    ];
    let out = build(&fx.input(parts.clone(), "", None)).unwrap();
    let m = out.manifest.clone().unwrap();
    assert_eq!(m.parts.iter().map(|q| q.pr).collect::<Vec<_>>(), vec![1, 4]);
    let reasons: Vec<(u64, Reason)> = m.left_out.iter().map(|l| (l.pr, l.reason)).collect();
    assert_eq!(
        reasons,
        vec![(2, Reason::Conflict), (3, Reason::Descendant)]
    );
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest(fx.path(), &m, &parts).unwrap();
    verify_result(fx.path(), &m.base, &out.result, Some(&m)).unwrap();
    let introduced = git::commits(fx.path(), &format!("{}..{}", m.base, m.train_sha)).unwrap();
    assert!(
        !introduced.contains(&p) && !introduced.contains(&c),
        "neither the parent's nor the child's commits ride"
    );

    // A hand-built train that merges the child alone brings the parent's commits back: refused by closure.
    let mut cheat = m.clone();
    cheat.parts.push(Part {
        pr: 3,
        head: c.clone(),
        parent: Some(Parent {
            pr: 2,
            head: p.clone(),
        }),
        own: Own::of(&[]),
        receipt: parts[2].receipt.clone(),
    });
    cheat.merges.push(m.train_sha.clone());
    let err = verify_manifest(fx.path(), &cheat, &parts).unwrap_err();
    assert!(err.starts_with("closure"), "{err}");
    // A manifest that declares the child a bottom part is not the registered dependency.
    let mut lie = m.clone();
    lie.parts[0].parent = Some(Parent {
        pr: 9,
        head: x.clone(),
    });
    assert!(verify_manifest(fx.path(), &lie, &parts)
        .unwrap_err()
        .starts_with("registered"));
}

#[test]
fn an_extra_commit_after_the_last_merge_and_a_wrong_order_are_refused() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    let parts = vec![part(1, &a, None), part(2, &b, None)];
    let m = build(&fx.input(parts.clone(), "", None))
        .unwrap()
        .manifest
        .unwrap();
    publish(fx.path(), "land/build/b1", &m).unwrap();
    let extra = fx.commit(&m.train_sha, "sneak.txt", "x\n");
    let mut tipped = m.clone();
    tipped.train_sha = extra;
    assert!(verify_manifest(fx.path(), &tipped, &parts)
        .unwrap_err()
        .starts_with("chain"));
    let mut swapped = m.clone();
    swapped.parts.swap(0, 1);
    swapped.merges.swap(0, 1);
    assert!(verify_manifest(fx.path(), &swapped, &parts).is_err());
}

#[test]
fn a_union_merge_attribute_replays_to_the_same_tree_with_the_recorded_inputs_and_not_without_them()
{
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "log.txt", "entry 0\nentry A\n");
    let b = fx.commit(&fx.base(), "log.txt", "entry 0\nentry B\n");
    let parts = vec![part(1, &a, None), part(2, &b, None)];
    // Without the attribute the second part conflicts with the first.
    let plain = build(&fx.input(parts.clone(), "", None)).unwrap();
    assert_eq!(plain.result.left_out[0].reason, Reason::Conflict);
    let attrs = "log.txt merge=union\n";
    let out = build(&fx.input(parts.clone(), attrs, None)).unwrap();
    let m = out.manifest.unwrap();
    assert_eq!(m.parts.len(), 2);
    assert_eq!(m.policy.attributes, attrs);
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest(fx.path(), &m, &parts).unwrap();
    let tree = g(
        fx.path(),
        &["rev-parse", &format!("{}^{{tree}}", m.train_sha)],
    );
    let merged = g(fx.path(), &["show", &format!("{}:log.txt", m.train_sha)]);
    assert!(
        merged.contains("entry A") && merged.contains("entry B"),
        "{tree}"
    );
    // The same replay with other inputs does not reproduce the tree.
    let mut other = m.clone();
    other.policy.attributes = String::new();
    assert!(verify_manifest(fx.path(), &other, &parts)
        .unwrap_err()
        .starts_with("replay"));
}

#[test]
fn an_oversized_stack_enters_as_a_dependency_closed_prefix() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&a, "b.txt", "b\n");
    let c = fx.commit(&b, "c.txt", "c\n");
    let parts = vec![
        part(1, &a, None),
        part(2, &b, Some((1, &a))),
        part(3, &c, Some((2, &b))),
    ];
    let out = build(&fx.input(parts.clone(), "", Some(2))).unwrap();
    let m = out.manifest.clone().unwrap();
    assert_eq!(m.parts.iter().map(|p| p.pr).collect::<Vec<_>>(), vec![1, 2]);
    assert_eq!(m.left_out.len(), 1);
    assert_eq!((m.left_out[0].pr, m.left_out[0].reason), (3, Reason::Cap));
    assert_eq!(m.graph, vec![(2, 1)]);
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest(fx.path(), &m, &parts).unwrap();
    verify_result(fx.path(), &m.base, &out.result, Some(&m)).unwrap();
    // A cap that was not reached cannot excuse a part.
    let mut bad = out.result.clone();
    bad.cap = Some(5);
    assert!(verify_result(fx.path(), &m.base, &bad, Some(&m))
        .unwrap_err()
        .starts_with("cap_not_proven"));
}

#[test]
fn the_child_of_a_landed_parent_enters_as_a_bottom_part() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let c = fx.commit(&a, "c.txt", "c\n");
    // The parent landed: main now contains it.
    g(fx.path(), &["merge", "-q", "--ff-only", &a]);
    let parts = vec![part(2, &c, Some((1, &a)))];
    let out = build(&fx.input(parts.clone(), "", None)).unwrap();
    let m = out.manifest.unwrap();
    assert_eq!(m.parts[0].own.count, 1, "only the child's own commit lands");
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest(fx.path(), &m, &parts).unwrap();
}

#[test]
fn a_build_that_merges_nothing_has_a_result_and_no_train() {
    let fx = Fx::new();
    let x = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "X", "line 3", "line 4", "line 5", "line 6"]),
    );
    g(fx.path(), &["merge", "-q", "--ff-only", &x]);
    let y = fx.commit(&fx.base(), "shared.txt", "\n");
    let _ = y;
    let base = fx.base();
    let p = fx.commit(
        &base,
        "shared.txt",
        &lines(&["line 1", "Y", "line 3", "line 4", "line 5", "line 6"]),
    );
    let _ = g(fx.path(), &["rev-parse", "HEAD"]);
    // `p` was cut from the new main and merges cleanly; build a conflicting one from the old base.
    let old = g(fx.path(), &["rev-parse", &format!("{x}^")]);
    let q = fx.commit(
        &old,
        "shared.txt",
        &lines(&["line 1", "Z", "line 3", "line 4", "line 5", "line 6"]),
    );
    let parts = vec![part(1, &q, None)];
    let out = build(&fx.input(parts, "", None)).unwrap();
    assert!(out.manifest.is_none());
    assert_eq!(out.result.left_out[0].reason, Reason::Conflict);
    assert_eq!(
        out.result.left_out[0].prefix, base,
        "without a train every prefix is the build's base"
    );
    verify_result(fx.path(), &base, &out.result, None).unwrap();
    let _ = p;
}

#[test]
fn a_result_is_refused_when_it_omits_a_part_names_a_wrong_prefix_or_lists_a_clean_part_as_conflict()
{
    let fx = Fx::new();
    let x = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "X", "line 3", "line 4", "line 5", "line 6"]),
    );
    let p = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let ok = fx.commit(&fx.base(), "ok.txt", "ok\n");
    let parts = vec![part(1, &x, None), part(2, &p, None), part(3, &ok, None)];
    let out = build(&fx.input(parts, "", None)).unwrap();
    let m = out.manifest.clone().unwrap();
    verify_result(fx.path(), &m.base, &out.result, Some(&m)).unwrap();
    let mut omit = out.result.clone();
    omit.snapshot.pop();
    assert!(verify_result(fx.path(), &m.base, &omit, Some(&m))
        .unwrap_err()
        .contains("omits"));
    let mut wrong = out.result.clone();
    wrong.left_out[0].prefix = m.base.clone();
    assert!(verify_result(fx.path(), &m.base, &wrong, Some(&m))
        .unwrap_err()
        .starts_with("wrong_prefix"));
    let mut clean = out.result.clone();
    clean.merged.retain(|pr| *pr != 3);
    clean.left_out.push(LeftOut {
        pr: 3,
        head: ok.clone(),
        reason: Reason::Conflict,
        prefix: m.merges[0].clone(),
    });
    let mut shrunk = m.clone();
    shrunk.parts.pop();
    shrunk.merges.pop();
    assert!(verify_result(fx.path(), &m.base, &clean, Some(&shrunk))
        .unwrap_err()
        .starts_with("conflict_not_reproduced"));
}

#[test]
fn the_builder_ignores_the_users_and_the_repositorys_git_configuration() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "log.txt", "entry 0\nentry A\n");
    let b = fx.commit(&fx.base(), "log.txt", "entry 0\nentry B\n");
    // A local configuration and an info/attributes file that would make the merge clean.
    g(
        fx.path(),
        &["config", "core.attributesFile", "/nonexistent"],
    );
    std::fs::create_dir_all(fx.path().join(".git/info")).unwrap();
    std::fs::write(
        fx.path().join(".git/info/attributes"),
        "log.txt merge=union\n",
    )
    .unwrap();
    let parts = vec![part(1, &a, None), part(2, &b, None)];
    let out = build(&fx.input(parts, "", None)).unwrap();
    assert_eq!(
        out.result.left_out.len(),
        1,
        "the repository's attributes do not change the result"
    );
}

#[test]
fn the_command_builds_nothing_while_main_is_red_or_pending_and_otherwise_writes_manifest_and_result(
) {
    use super::cli::{build_train, Build};
    use super::main_state::State;
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let out = tempfile::tempdir().unwrap();
    let job = Build {
        repo: fx.path().to_owned(),
        base: fx.base(),
        name: "b1".into(),
        parts: vec![part(1, &a, None)],
        attributes: String::new(),
        cap: None,
        out: out.path().to_owned(),
        branch: "land/build/b1".into(),
        resolutions: vec![],
        protected: vec![],
        open: BuildOpen {
            build: "b1".into(),
            policy_version: git::POLICY_VERSION.into(),
            base: fx.base(),
            grant_digest: "g".into(),
            snapshot_position: 3,
        },
    };
    let tip = job.base.clone();
    for (state, word) in [
        (State::Red { blocking: vec![] }, "main_red"),
        (State::Pending, "main_pending"),
    ] {
        let err = build_train(&job, &state, &tip).unwrap_err();
        assert!(err.starts_with(word), "{err}");
    }
    assert!(
        !out.path().join("b1.manifest.json").exists(),
        "no manifest is frozen"
    );
    let lines = build_train(&job, &State::Green, &tip).unwrap();
    assert!(lines[0].contains("1 merged"));
    let m: Manifest =
        serde_json::from_slice(&std::fs::read(out.path().join("b1.manifest.json")).unwrap())
            .unwrap();
    assert_eq!(g(fx.path(), &["rev-parse", "land/build/b1"]), m.train_sha);
    // The build record was written before the merges.
    let open: BuildOpen =
        serde_json::from_slice(&std::fs::read(out.path().join("b1.open.json")).unwrap()).unwrap();
    assert_eq!(
        (open.grant_digest.as_str(), open.snapshot_position),
        ("g", 3)
    );
    // A tip that is not the base (main moved, or another repository was read): refused, nothing published.
    let moved = Build {
        name: "b2".into(),
        branch: "land/build/b2".into(),
        open: BuildOpen {
            build: "b2".into(),
            ..job.open.clone()
        },
        ..job.clone()
    };
    let err = build_train(
        &moved,
        &State::Green,
        "0000000000000000000000000000000000000000",
    )
    .unwrap_err();
    assert!(err.starts_with("stale_base"), "{err}");
    assert!(g(fx.path(), &["for-each-ref", "refs/heads/land/build/b2"]).is_empty());
    // A build record of another policy version or base is not this build's.
    let bad_open = Build {
        open: BuildOpen {
            policy_version: "other".into(),
            ..job.open.clone()
        },
        ..job.clone()
    };
    assert!(build_train(&bad_open, &State::Green, &tip).is_err());
}

#[test]
fn verify_checks_the_result_and_the_freeze_against_the_recomputed_selection() {
    use super::cli::adopted_from_log;
    let fx = Fx::new();
    let x = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "X", "line 3", "line 4", "line 5", "line 6"]),
    );
    let p = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    let out = build(&fx.input(parts.clone(), "", None)).unwrap();
    let open = BuildOpen {
        build: "b1".into(),
        policy_version: git::POLICY_VERSION.into(),
        base: fx.base(),
        grant_digest: "g".into(),
        snapshot_position: 1,
    };
    let freeze = Freeze {
        policy_version: open.policy_version.clone(),
        base: open.base.clone(),
        grant_digest: "g".into(),
        selection: parts.clone(),
    };
    assert_eq!(check_freeze(&open, &out.result.snapshot, &freeze), Ok(()));
    let mut moved = freeze.clone();
    moved.selection[1].head = x.clone();
    assert_eq!(
        check_freeze(&open, &out.result.snapshot, &moved),
        Err(FreezeRefusal::SelectionChanged)
    );
    // Adopted results come from the replay of a log, a missing file is an error.
    assert!(adopted_from_log("/nonexistent/log.json", "b1").is_err());
}

/// A conflicting pair: x and p edit the same line. Returns (x, p, prefix commit M1 of the first build).
fn conflicting(fx: &Fx) -> (String, String, String) {
    let x = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "X", "line 3", "line 4", "line 5", "line 6"]),
    );
    let p = fx.commit(
        &fx.base(),
        "shared.txt",
        &lines(&["line 1", "P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let first = build(&fx.input(vec![part(1, &x, None), part(2, &p, None)], "", None)).unwrap();
    let m1 = first.manifest.unwrap().merges[0].clone();
    assert_eq!(
        first.result.left_out[0].prefix, m1,
        "the request names the prefix the part conflicted on"
    );
    (x, p, m1)
}

/// The writer's candidate: the merge of p onto m1 with the conflict resolved by `text` in `file`.
fn resolve(fx: &Fx, m1: &str, p: &str, file: &str, text: &str) -> String {
    let d = fx.path();
    g(d, &["checkout", "-q", "--detach", m1]);
    let _ = Command::new("git")
        .current_dir(d)
        .args(["merge", "--no-ff", "-m", "resolved", p])
        .env("GIT_AUTHOR_NAME", "w")
        .env("GIT_AUTHOR_EMAIL", "w@example.invalid")
        .env("GIT_COMMITTER_NAME", "w")
        .env("GIT_COMMITTER_EMAIL", "w@example.invalid")
        .output()
        .unwrap();
    std::fs::write(d.join(file), text).unwrap();
    g(d, &["add", "-A"]);
    g(d, &["commit", "-q", "-m", "resolved"]);
    let r = g(d, &["rev-parse", "HEAD"]);
    g(d, &["checkout", "-q", "main"]);
    r
}

fn adopted(m1: &str, p: &str, commit: &str) -> super::resolve::Adopted {
    super::resolve::Adopted {
        request: "r1".into(),
        build: "b1".into(),
        position: 2,
        part: 2,
        head: p.into(),
        onto: m1.into(),
        submission: "s2".into(),
        commit: commit.into(),
    }
}

#[test]
fn a_conflicting_part_with_an_adopted_resolution_is_merged_and_listed_as_a_resolution() {
    let fx = Fx::new();
    let (x, p, m1) = conflicting(&fx);
    let r = resolve(
        &fx,
        &m1,
        &p,
        "shared.txt",
        &lines(&["line 1", "X and P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let a = adopted(&m1, &p, &r);
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    let mut input = fx.input(parts.clone(), "", None);
    input.resolutions = vec![a.clone()];
    let out = build(&input).unwrap();
    let m = out.manifest.clone().unwrap();
    assert_eq!(m.parts.len(), 2);
    assert!(
        m.left_out.is_empty(),
        "resolved instead of left out as conflict"
    );
    assert_eq!(
        m.resolution,
        vec![Resolution {
            part: 2,
            submission: "s2".into(),
            commit: r.clone()
        }]
    );
    assert_eq!(m.train_sha, r);
    publish(fx.path(), "land/build/b1", &m).unwrap();
    verify_manifest_with(fx.path(), &m, &parts, std::slice::from_ref(&a), &[]).unwrap();
    verify_result(fx.path(), &m.base, &out.result, Some(&m)).unwrap();
    // The gate refuses the same manifest without an adopted result of this build, or for another prefix.
    assert!(verify_manifest_with(fx.path(), &m, &parts, &[], &[])
        .unwrap_err()
        .starts_with("resolution"));
    let mut elsewhere = a.clone();
    elsewhere.onto = fx.base();
    assert!(
        verify_manifest_with(fx.path(), &m, &parts, &[elsewhere], &[])
            .unwrap_err()
            .starts_with("resolution")
    );
    // A plain verify has no adopted results: the resolved manifest is refused there too.
    assert!(verify_manifest(fx.path(), &m, &parts).is_err());
}

#[test]
fn a_resolution_with_wrong_parents_or_a_protected_path_is_not_adopted_by_the_builder() {
    let fx = Fx::new();
    let (x, p, m1) = conflicting(&fx);
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    // Wrong parents: a commit on top of m1 that does not merge p.
    let wrong = fx.commit(&m1, "shared.txt", "resolved by hand\n");
    let mut input = fx.input(parts.clone(), "", None);
    input.resolutions = vec![adopted(&m1, &p, &wrong)];
    let out = build(&input).unwrap();
    assert_eq!(out.result.left_out.len(), 1, "left out as conflict");
    assert!(out.manifest.unwrap().resolution.is_empty());
    assert!(check_resolution(fx.path(), &adopted(&m1, &p, &wrong), &[])
        .unwrap_err()
        .starts_with("wrong parents"));
    // A resolution that edits a protected path is refused, `.gitattributes` always.
    let r = resolve(&fx, &m1, &p, ".gitattributes", "* merge=union\n");
    let r_fixed = {
        // The resolution also resolves the conflict itself.
        let d = fx.path();
        g(d, &["checkout", "-q", "--detach", &r]);
        std::fs::write(d.join("shared.txt"), "ok\n").unwrap();
        g(d, &["add", "-A"]);
        g(d, &["commit", "-q", "--amend", "-m", "resolved"]);
        let c = g(d, &["rev-parse", "HEAD"]);
        g(d, &["checkout", "-q", "main"]);
        c
    };
    assert!(
        check_resolution(fx.path(), &adopted(&m1, &p, &r_fixed), &[])
            .unwrap_err()
            .contains(".gitattributes")
    );
    let ok = resolve(&fx, &m1, &p, "shared.txt", "fine\n");
    assert!(check_resolution(fx.path(), &adopted(&m1, &p, &ok), &[]).is_ok());
    assert!(
        check_resolution(fx.path(), &adopted(&m1, &p, &ok), &["shared".to_owned()])
            .unwrap_err()
            .contains("protected path")
    );
}

#[test]
fn a_resolution_entry_for_a_part_that_merges_cleanly_is_refused() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    let parts = vec![part(1, &a, None), part(2, &b, None)];
    let m = build(&fx.input(parts.clone(), "", None))
        .unwrap()
        .manifest
        .unwrap();
    publish(fx.path(), "land/build/b1", &m).unwrap();
    let mut forged = m.clone();
    forged.resolution = vec![Resolution {
        part: 2,
        submission: "s2".into(),
        commit: m.merges[1].clone(),
    }];
    let ad = super::resolve::Adopted {
        request: "r1".into(),
        build: "b1".into(),
        position: 2,
        part: 2,
        head: b.clone(),
        onto: m.merges[0].clone(),
        submission: "s2".into(),
        commit: m.merges[1].clone(),
    };
    let err = verify_manifest_with(fx.path(), &forged, &parts, &[ad], &[]).unwrap_err();
    assert!(err.contains("merges cleanly"), "{err}");
}

#[test]
fn an_adopted_result_of_another_build_or_position_is_neither_used_nor_accepted() {
    let fx = Fx::new();
    let (x, p, m1) = conflicting(&fx);
    let r = resolve(
        &fx,
        &m1,
        &p,
        "shared.txt",
        &lines(&["line 1", "X and P", "line 3", "line 4", "line 5", "line 6"]),
    );
    let parts = vec![part(1, &x, None), part(2, &p, None)];
    for tweak in [
        (|a: &mut super::resolve::Adopted| a.build = "other-build".into())
            as fn(&mut super::resolve::Adopted),
        |a| a.position = 1,
    ] {
        let mut wrong = adopted(&m1, &p, &r);
        tweak(&mut wrong);
        let mut input = fx.input(parts.clone(), "", None);
        input.resolutions = vec![wrong.clone()];
        let out = build(&input).unwrap();
        assert_eq!(
            out.result.left_out.len(),
            1,
            "the builder does not use it: the part is left out as conflict"
        );
        assert!(out.manifest.unwrap().resolution.is_empty());
        // The same result cannot back a manifest that lists the resolution.
        let mut good = fx.input(parts.clone(), "", None);
        good.resolutions = vec![adopted(&m1, &p, &r)];
        let m = build(&good).unwrap().manifest.unwrap();
        publish(fx.path(), "land/build/b1", &m).unwrap();
        assert!(verify_manifest_with(fx.path(), &m, &parts, &[wrong], &[])
            .unwrap_err()
            .starts_with("resolution"));
    }
}

#[test]
fn the_open_record_freeze_manifest_and_result_must_belong_to_one_build_base_and_policy() {
    use super::cli::bind_artifacts;
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let out = build(&fx.input(vec![part(1, &a, None)], "", None)).unwrap();
    let m = out.manifest.unwrap();
    let open = BuildOpen {
        build: "b1".into(),
        policy_version: git::POLICY_VERSION.into(),
        base: fx.base(),
        grant_digest: "g".into(),
        snapshot_position: 1,
    };
    let freeze = Freeze {
        policy_version: open.policy_version.clone(),
        base: open.base.clone(),
        grant_digest: "g".into(),
        selection: vec![part(1, &a, None)],
    };
    assert!(bind_artifacts(&open, &freeze, &m, &out.result).is_ok());
    let mut other_build = open.clone();
    other_build.build = "b2".into();
    assert!(bind_artifacts(&other_build, &freeze, &m, &out.result).is_err());
    let mut other_base = open.clone();
    other_base.base = "0".repeat(40);
    let mut other_freeze_base = freeze.clone();
    other_freeze_base.base = "0".repeat(40);
    assert!(bind_artifacts(&other_base, &freeze, &m, &out.result).is_err());
    assert!(bind_artifacts(&open, &other_freeze_base, &m, &out.result).is_err());
    let mut other_policy = open.clone();
    other_policy.policy_version = "other".into();
    let mut other_freeze_policy = freeze.clone();
    other_freeze_policy.policy_version = "other".into();
    assert!(bind_artifacts(&other_policy, &freeze, &m, &out.result).is_err());
    assert!(bind_artifacts(&open, &other_freeze_policy, &m, &out.result).is_err());
    let mut other_result = out.result.clone();
    other_result.build = "b9".into();
    assert!(bind_artifacts(&open, &freeze, &m, &other_result).is_err());
}

#[path = "build_receipt_tests.rs"]
mod receipt_tests;
