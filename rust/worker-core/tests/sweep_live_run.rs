#![cfg(target_os = "linux")]
//! `live_run` is ownership by an unfinished run record whose checkout or attempt is at or inside
//! the tree; a pin, a reserved name or a run in an ancestor directory is not one.
use pg_reap::{classify, parse_porcelain, Classification};
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::{run_with_authority, Options},
    reaper::reap_with_authority,
    sweep_facts::{facts, Claims, Protection, Pull},
};
use std::{
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

fn git(repo: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::git_test_support::command(repo)
        .args([
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

fn protection(guards: Guards) -> Protection {
    Protection {
        guards,
        claims: Claims {
            paths: vec![],
            branches: vec![],
        },
        units: vec![],
        unit_refs: vec![],
        cwds: vec![],
    }
}

/// Write `runs/<id>/record.json`; `extra` carries survivors and attempts.
fn record(state: &Path, id: &str, status: &str, cwd: &Path, extra: serde_json::Value) {
    let dir = state.join("runs").join(id);
    fs::create_dir_all(&dir).unwrap();
    let mut row = serde_json::json!({"status": status, "cwd": cwd});
    for (key, value) in extra.as_object().unwrap() {
        row[key] = value.clone();
    }
    fs::write(dir.join("record.json"), row.to_string()).unwrap();
}

fn age(tree: &Path, seconds: u64) {
    let dir = git(tree, &["rev-parse", "--absolute-git-dir"]);
    for name in ["HEAD", "index"] {
        fs::File::options()
            .write(true)
            .open(Path::new(dir.trim()).join(name))
            .unwrap()
            .set_times(
                fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(seconds)),
            )
            .unwrap();
    }
}

struct Fixture {
    _tmp: tempfile::TempDir,
    repo: PathBuf,
    root: PathBuf,
    state: PathBuf,
    pins: PathBuf,
}

impl Fixture {
    /// A merged, pushed, clean and old `topic` tree under `root`, outside the run-state roots.
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        let root = tmp.path().join("trees");
        let state = tmp.path().join("state");
        let remote = tmp.path().join("remote");
        for path in [&repo, &root, &state, &remote] {
            fs::create_dir(path).unwrap();
        }
        git(&remote, &["init", "--bare", "-q"]);
        git(&repo, &["init", "-qb", "main"]);
        fs::write(repo.join("one"), "one").unwrap();
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-qm", "initial"]);
        git(
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        let tree = root.join("topic");
        git(
            &repo,
            &["worktree", "add", "-qb", "topic", tree.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "topic"]);
        age(&tree, 8 * 86_400);
        let pins = state.join("pins.json");
        fs::write(&pins, "[]").unwrap();
        Self {
            _tmp: tmp,
            repo,
            root,
            state,
            pins,
        }
    }

    fn options(&self) -> Options<'_> {
        Options {
            repo: &self.repo,
            roots: std::slice::from_ref(&self.root),
            targets: &[],
            remote_targets: &[],
            idle_minutes: 60,
            state: &self.state,
            pins: &self.pins,
            claims_reader: None,
            apply: false,
            include_merged: false,
            delete_remote: false,
        }
    }
}

fn authority(options: &Options<'_>, _: &str) -> std::io::Result<(Protection, Vec<Pull>)> {
    Ok((
        protection(Guards::load(options.pins, options.state)?),
        vec![],
    ))
}

#[test]
fn finished_runs_pins_and_ancestor_runs_are_not_live_run() {
    let f = Fixture::new();
    let tree = f.root.join("topic");
    let trees = parse_porcelain(&git(&f.repo, &["worktree", "list", "--porcelain"])).unwrap();
    let current = trees.iter().find(|w| w.path.ends_with("topic")).unwrap();
    let now = SystemTime::now() + Duration::from_secs(90000);
    let flags = |guards: Guards| {
        let x = facts(&f.repo, current, &protection(guards), &[], now).unwrap();
        (x.live_run, x.pinned)
    };
    let none = serde_json::json!({});

    // A finished run on the tree is not live; an unfinished run on it is.
    record(&f.state, "run-done", "completed", &tree, none.clone());
    assert_eq!(
        flags(Guards::load(&f.pins, &f.state).unwrap()),
        (false, false)
    );
    record(&f.state, "run-live", "running", &tree.join("rust"), none);
    assert_eq!(
        flags(Guards::load(&f.pins, &f.state).unwrap()),
        (true, false)
    );

    // An unfinished run in the repository root does not own the trees below it.
    let ancestor = Guards {
        pinned: vec![],
        live: vec![f.repo.clone(), f.root.clone()],
    };
    assert_eq!(flags(ancestor), (false, false));

    // A pin protects the tree under its own reason and is not counted as a run.
    let pin = Guards {
        pinned: vec![tree.clone()],
        live: vec![],
    };
    assert_eq!(flags(pin), (false, true));

    // A reserved branch name on an ordinary checkout is a pin, not a run.
    let reserved = f.root.join("ordinary");
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "-qb",
            "live-keeper",
            reserved.to_str().unwrap(),
        ],
    );
    let trees = parse_porcelain(&git(&f.repo, &["worktree", "list", "--porcelain"])).unwrap();
    let current = trees.iter().find(|w| w.path.ends_with("ordinary")).unwrap();
    let x = facts(&f.repo, current, &protection(Guards::default()), &[], now).unwrap();
    assert_eq!((x.live_run, x.pinned), (false, true));
    assert_eq!(
        classify(current, &x, &trees),
        Classification::Protected { reason: "pinned" }
    );
}

#[test]
fn terminal_run_with_survivors_still_owns_the_worktree() {
    let f = Fixture::new();
    let tree = f.root.join("topic");
    let options = f.options();
    let sweep = || run_with_authority(&options, authority).unwrap();
    assert!(sweep().contains(&format!("would-remove {}", tree.display())));

    // Only an ancestor checkout is recorded: the tree stays removable.
    record(
        &f.state,
        "ancestor",
        "running",
        &f.root,
        serde_json::json!({}),
    );
    let report = sweep();
    assert!(report.contains("would-remove"), "{report}");
    assert!(!report.contains("live-run"), "{report}");

    // A finished run without survivors owns nothing.
    fs::create_dir(tree.join("attempt")).unwrap();
    let attempt = serde_json::json!({"worktree_attempts": [{"path": tree.join("attempt")}]});
    record(&f.state, "ancestor", "completed", &f.root, attempt.clone());
    assert!(sweep().contains("would-remove"));

    // The same finished run with survivors owns the tree through its attempt inside it.
    let mut held = attempt;
    held["survivors"] = serde_json::json!([4242]);
    record(&f.state, "ancestor", "completed", &f.root, held);
    let report = sweep();
    assert!(
        report.contains("live-run") && !report.contains("would-remove"),
        "{report}"
    );
}

#[test]
fn ancestor_only_run_blocks_quarantine() {
    let f = Fixture::new();
    let tree = f.root.join("topic");
    let options = f.options();
    let plan = reap_with_authority(&options, None, authority).unwrap();
    let row = plan
        .lines()
        .find(|line| line.contains("would-quarantine"))
        .unwrap_or_else(|| panic!("{plan}"));
    let plan = format!("{row}\n");

    // A run record naming only an ancestor of the tree appeared after the plan was issued.
    record(
        &f.state,
        "ancestor",
        "running",
        &f.root,
        serde_json::json!({}),
    );
    let report = reap_with_authority(&options, None, authority).unwrap();
    assert!(!report.contains("would-quarantine"), "{report}");
    assert!(report.contains("original path is in use"), "{report}");
    let out = reap_with_authority(&options, Some(("D-123", &plan)), authority).unwrap();
    assert!(!out.contains("quarantined") && tree.exists(), "{out}");
    assert!(!f.state.join("reap-quarantine").exists());
}

#[test]
fn deletion_protection_includes_every_pin_rule_and_adds_run_overlap() {
    let tmp = tempfile::tempdir().unwrap();
    let (pin, run) = (tmp.path().join("pin"), tmp.path().join("run"));
    let guards = Guards {
        pinned: vec![pin.clone()],
        live: vec![run.clone()],
    };
    for path in [
        pin.clone(),
        pin.join("below"),
        tmp.path().to_path_buf(),
        tmp.path().join("plangraph-md-sync/x"),
        tmp.path().join("live-keeper"),
    ] {
        assert!(guards.pin_protects(&path), "{}", path.display());
        assert!(guards.protects(&path), "{}", path.display());
    }
    // Without pins, a run protects broadly in both directions: the run's own path, a path below
    // it and an unpinned parent that holds it. Only the run path and the parent are owned.
    let runs = Guards {
        pinned: vec![],
        live: vec![run.clone()],
    };
    let parent = tmp.path().to_path_buf();
    for (path, owned) in [(&run, true), (&parent, true), (&run.join("below"), false)] {
        assert!(runs.protects(path), "{}", path.display());
        assert_eq!(runs.live_run_owns(path), owned, "{}", path.display());
        assert!(!runs.pin_protects(path), "{}", path.display());
    }
    let other = tmp.path().join("other");
    assert!(!runs.protects(&other) && !guards.protects(&other) && !guards.pin_protects(&other));
}
