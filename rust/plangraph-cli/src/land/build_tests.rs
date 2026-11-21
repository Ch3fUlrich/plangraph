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
    };
    for (state, word) in [
        (State::Red { blocking: vec![] }, "main_red"),
        (State::Pending, "main_pending"),
    ] {
        let err = build_train(&job, &state).unwrap_err();
        assert!(err.starts_with(word), "{err}");
    }
    assert!(
        !out.path().join("b1.manifest.json").exists(),
        "no manifest is frozen"
    );
    let lines = build_train(&job, &State::Green).unwrap();
    assert!(lines[0].contains("1 merged"));
    let m: Manifest =
        serde_json::from_slice(&std::fs::read(out.path().join("b1.manifest.json")).unwrap())
            .unwrap();
    assert_eq!(g(fx.path(), &["rev-parse", "land/build/b1"]), m.train_sha);
}
