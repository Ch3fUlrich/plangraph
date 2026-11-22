use super::git;
use super::registrar::*;
use super::train::Parent;
use std::path::Path;
use std::process::Command;

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

struct Fx(tempfile::TempDir);

impl Fx {
    fn new() -> Fx {
        let d = tempfile::tempdir().unwrap();
        g(d.path(), &["init", "-q", "-b", "main"]);
        std::fs::write(d.path().join("f.txt"), "base\n").unwrap();
        g(d.path(), &["add", "-A"]);
        g(d.path(), &["commit", "-q", "-m", "base"]);
        Fx(d)
    }
    fn p(&self) -> &Path {
        self.0.path()
    }
    fn base(&self) -> String {
        g(self.p(), &["rev-parse", "main"])
    }
    fn commit(&self, from: &str, file: &str, text: &str) -> String {
        g(self.p(), &["checkout", "-q", "--detach", from]);
        std::fs::write(self.p().join(file), text).unwrap();
        g(self.p(), &["add", "-A"]);
        g(self.p(), &["commit", "-q", "-m", file]);
        let h = g(self.p(), &["rev-parse", "HEAD"]);
        g(self.p(), &["checkout", "-q", "main"]);
        h
    }
    fn merge(&self, into: &str, other: &str) -> String {
        g(self.p(), &["checkout", "-q", "--detach", into]);
        g(self.p(), &["merge", "-q", "--no-ff", "-m", "merge", other]);
        let h = g(self.p(), &["rev-parse", "HEAD"]);
        g(self.p(), &["checkout", "-q", "main"]);
        h
    }
}

fn pr(n: u64, branch: &str, base_ref: &str, ready: Option<&str>, seen: &[&str]) -> OpenPr {
    OpenPr {
        pr: n,
        head_branch: branch.into(),
        base_ref: base_ref.into(),
        ready: ready.map(Into::into),
        seen: seen.iter().map(|s| s.to_string()).collect(),
        consumed: None,
    }
}

#[test]
fn a_bottom_part_gets_the_merge_base_and_its_own_commits() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let me = pr(1, "a", "main", None, &[&a]);
    let Registration::Ready(d) = register(
        fx.p(),
        &fx.base(),
        "main",
        &me,
        &a,
        std::slice::from_ref(&me),
    )
    .unwrap() else {
        panic!()
    };
    assert_eq!((d.parent, d.diff_base, d.own.count), (None, fx.base(), 1));
}

#[test]
fn a_child_rebuilt_by_rebase_or_by_merging_the_parent_in_is_registered_on_the_parent_head() {
    let fx = Fx::new();
    let p1 = fx.commit(&fx.base(), "a.txt", "a\n");
    let p2 = fx.commit(&p1, "a2.txt", "a2\n");
    let parent = pr(1, "pa", "main", Some(&p2), &[&p1, &p2]);
    let on_new = fx.commit(&p2, "c.txt", "c\n");
    let merged = fx.merge(&fx.commit(&p1, "c2.txt", "c2\n"), &p2);
    for head in [&on_new, &merged] {
        let child = pr(2, "pc", "pa", None, &[head]);
        let Registration::Ready(d) = register(
            fx.p(),
            &fx.base(),
            "main",
            &child,
            head,
            &[parent.clone(), child.clone()],
        )
        .unwrap() else {
            panic!()
        };
        assert_eq!(
            d.parent,
            Some(Parent {
                pr: 1,
                head: p2.clone()
            })
        );
        assert_eq!(d.diff_base, p2);
    }
}

#[test]
fn a_child_that_does_not_contain_the_parents_ready_head_is_behind_parent() {
    let fx = Fx::new();
    let p1 = fx.commit(&fx.base(), "a.txt", "a\n");
    let p2 = fx.commit(&p1, "a2.txt", "a2\n");
    let c = fx.commit(&p1, "c.txt", "c\n");
    let parent = pr(1, "pa", "main", Some(&p2), &[&p1, &p2]);
    let child = pr(2, "pc", "pa", None, &[&c]);
    let r = register(
        fx.p(),
        &fx.base(),
        "main",
        &child,
        &c,
        &[parent.clone(), child.clone()],
    )
    .unwrap();
    assert_eq!(r, Registration::Refused(Refusal::BehindParent));
    // A parent without a READY head blocks it too.
    let unready = pr(1, "pa", "main", None, &[&p1]);
    assert_eq!(
        register(
            fx.p(),
            &fx.base(),
            "main",
            &child,
            &c,
            &[unready, child.clone()]
        )
        .unwrap(),
        Registration::Refused(Refusal::BehindParent)
    );
    // And the parent's new head ends the READY entry of the child that sits on the old one.
    assert_eq!(
        children_to_unready(fx.p(), &parent, &p2, &[(2, c.clone())]).unwrap(),
        vec![2]
    );
}

#[test]
fn a_rewritten_parent_with_the_old_commits_merged_into_the_child_is_superseded_history() {
    let fx = Fx::new();
    let old = fx.commit(&fx.base(), "a.txt", "old\n");
    let rewritten = fx.commit(&fx.base(), "a_new.txt", "new\n");
    let child_on_old = fx.commit(&old, "c.txt", "c\n");
    let merged_new = fx.merge(&child_on_old, &rewritten);
    let parent = pr(1, "pa", "main", Some(&rewritten), &[&old, &rewritten]);
    let child = pr(2, "pc", "pa", None, &[&merged_new]);
    // The merge conflicts in content terms only on a real conflict; here the history alone decides.
    let r = register(
        fx.p(),
        &fx.base(),
        "main",
        &child,
        &merged_new,
        &[parent.clone(), child.clone()],
    )
    .unwrap();
    assert_eq!(r, Registration::Refused(Refusal::SupersededParentHistory));
    // Rebased onto the rewritten head it is accepted.
    let rebased = fx.commit(&rewritten, "c.txt", "c\n");
    let child = pr(2, "pc", "pa", None, &[&rebased]);
    assert!(matches!(
        register(
            fx.p(),
            &fx.base(),
            "main",
            &child,
            &rebased,
            &[parent, child.clone()]
        )
        .unwrap(),
        Registration::Ready(_)
    ));
}

#[test]
fn the_child_of_a_landed_parent_registers_as_a_bottom_part_on_the_landed_head() {
    let fx = Fx::new();
    let orig = fx.base();
    let a = fx.commit(&orig, "a.txt", "a\n");
    let c = fx.commit(&a, "c.txt", "c\n");
    g(fx.p(), &["merge", "-q", "--ff-only", &a]);
    let mut parent = pr(1, "pa", "main", None, &[&a]);
    parent.consumed = Some(a.clone());
    let child = pr(2, "pc", "pa", None, &[&c]);
    let Registration::Ready(d) = register(
        fx.p(),
        &fx.base(),
        "main",
        &child,
        &c,
        &[parent.clone(), child.clone()],
    )
    .unwrap() else {
        panic!()
    };
    assert_eq!(
        (d.parent, d.diff_base.as_str(), d.own.count),
        (None, a.as_str(), 1)
    );
    // A child that does not contain the landed head is behind_parent.
    let stray = fx.commit(&orig, "s.txt", "s\n");
    let stray_pr = pr(3, "ps", "pa", None, &[&stray]);
    assert_eq!(
        register(
            fx.p(),
            &fx.base(),
            "main",
            &stray_pr,
            &stray,
            &[parent, stray_pr.clone()]
        )
        .unwrap(),
        Registration::Refused(Refusal::BehindParent)
    );
}

#[test]
fn a_head_already_in_the_landing_base_is_contained_never_ready() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    g(fx.p(), &["merge", "-q", "--ff-only", &a]);
    let me = pr(1, "a", "main", None, &[&a]);
    assert_eq!(
        register(
            fx.p(),
            &fx.base(),
            "main",
            &me,
            &a,
            std::slice::from_ref(&me)
        )
        .unwrap(),
        Registration::Contained
    );
}

#[test]
fn an_unknown_base_ref_is_refused() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let me = pr(1, "a", "nowhere", None, &[&a]);
    assert_eq!(
        register(
            fx.p(),
            &fx.base(),
            "main",
            &me,
            &a,
            std::slice::from_ref(&me)
        )
        .unwrap(),
        Registration::Refused(Refusal::UnknownBase)
    );
}

#[test]
fn pins_keep_every_observed_head_and_the_builder_reads_the_ready_pins() {
    let fx = Fx::new();
    let a = fx.commit(&fx.base(), "a.txt", "a\n");
    let b = fx.commit(&fx.base(), "b.txt", "b\n");
    pin(fx.p(), 7, &a, false).unwrap();
    pin(fx.p(), 8, &b, true).unwrap();
    g(fx.p(), &["gc", "-q", "--prune=now"]);
    assert_eq!(
        git::out(
            fx.p(),
            &["rev-parse", &format!("refs/heads/land/seen/7/{a}")]
        )
        .unwrap(),
        a
    );
    assert_eq!(ready_pins(fx.p()).unwrap(), vec![(8, b)]);
}
