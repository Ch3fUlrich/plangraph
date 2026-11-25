#![cfg(unix)]
use pg_worker_core::{
    disk_hygiene::Guards,
    merged_sweep::Options,
    sweep_facts::{Claims, Protection, Pull},
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
struct Fixture {
    _tmp: tempfile::TempDir,
    repo: PathBuf,
    root: PathBuf,
    state: PathBuf,
    sha: String,
    pins: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let repo = tmp.path().join("repo");
        let root = tmp.path().join("trees");
        let state = tmp.path().join("state");
        for path in [&repo, &root, &state] {
            fs::create_dir(path).unwrap();
        }
        git(&repo, &["init", "-qb", "main"]);
        fs::create_dir(repo.join("tracked-subdirectory")).unwrap();
        fs::write(repo.join("tracked-subdirectory/one"), "one").unwrap();
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-qm", "initial"]);
        let remote = tmp.path().join("remote");
        fs::create_dir(&remote).unwrap();
        git(&remote, &["init", "--bare", "-q"]);
        git(
            &repo,
            &["remote", "add", "origin", remote.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        git(
            &repo,
            &[
                "worktree",
                "add",
                "-qb",
                "topic",
                root.join("topic").to_str().unwrap(),
            ],
        );
        git(&repo, &["push", "-q", "origin", "topic"]);
        let tree_git = git(&root.join("topic"), &["rev-parse", "--absolute-git-dir"]);
        for name in ["HEAD", "index"] {
            fs::File::options()
                .write(true)
                .open(Path::new(tree_git.trim()).join(name))
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(SystemTime::now() - Duration::from_secs(90000)),
                )
                .unwrap();
        }
        let pins = state.join("pins.json");
        fs::write(&pins, "[]").unwrap();
        let sha = git(&repo, &["rev-parse", "HEAD"]).trim().to_string();
        Self {
            _tmp: tmp,
            repo,
            root,
            state,
            sha,
            pins,
        }
    }
}
fn authority(options: &Options<'_>, _: &str) -> std::io::Result<(Protection, Vec<Pull>)> {
    Ok((
        Protection {
            guards: Guards::load(options.pins, options.state)?,
            claims: Claims {
                paths: vec![],
                branches: vec![],
                unbound_runs: 0,
            },
            units: vec![],
            unit_refs: vec![],
            cwds: vec![],
        },
        vec![],
    ))
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

#[test]
fn reaper_requires_exact_approval_and_quarantines_without_removing_the_branch() {
    use pg_worker_core::reaper::reap_with_authority;
    let f = Fixture::new();
    let tree = f.root.join("topic");
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        targets: &[],
        remote_targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: false,
        include_merged: false,
        delete_remote: false,
    };
    let report = reap_with_authority(&opts, None, authority).unwrap();
    assert!(report.contains("idle-below-clean-threshold"), "{report}");
    age(&tree, 8 * 86_400);
    fs::write(tree.join("local"), "keep").unwrap();
    let report = reap_with_authority(&opts, None, authority).unwrap();
    assert!(report.contains("dirty"), "{report}");
    fs::remove_file(tree.join("local")).unwrap();
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let row = report
        .lines()
        .find(|line| line.contains("would-quarantine"))
        .unwrap();
    let plan = format!("{row}\n");
    assert!(tree.exists());
    assert!(!f.state.join("reap-quarantine").exists());
    assert!(reap_with_authority(&opts, Some(("bad-id", &plan)), authority).is_err());
    let out = reap_with_authority(&opts, Some(("D-123", "")), authority).unwrap();
    assert!(
        out.contains("outside approved plan") && tree.exists(),
        "{out}"
    );
    let out = reap_with_authority(&opts, Some(("D-123", &plan)), authority).unwrap();
    assert!(out.contains("quarantined") && !tree.exists(), "{out}");
    assert!(git(&f.repo, &["branch", "--list", "topic"]).contains("topic"));
    let report = reap_with_authority(&opts, None, authority).unwrap();
    assert!(report.contains("quarantine-hold"), "{report}");
}

#[test]
fn native_terminal_run_logs_are_separate_from_worktree_ownership() {
    use pg_worker_core::reaper::reap_with_authority;
    let f = Fixture::new();
    let run = f.state.join("runs/run-fixture");
    fs::create_dir_all(&run).unwrap();
    let record = run.join("record.json");
    fs::write(
        &record,
        serde_json::json!({
            "status":"completed", "cwd":run.join("worktree"), "repo":f.repo, "final_head":f.sha,
        })
        .to_string(),
    )
    .unwrap();
    fs::File::options()
        .write(true)
        .open(&record)
        .unwrap()
        .set_times(
            fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(8 * 86_400)),
        )
        .unwrap();
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        targets: &[],
        remote_targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: false,
        include_merged: false,
        delete_remote: false,
    };
    fs::create_dir(run.join("worktree")).unwrap();
    let out = reap_with_authority(&opts, None, authority).unwrap();
    assert!(out.contains("checkout still present"), "{out}");
    fs::remove_dir(run.join("worktree")).unwrap();
    fs::File::open(&run)
        .unwrap()
        .set_times(
            fs::FileTimes::new().set_modified(SystemTime::now() - Duration::from_secs(8 * 86_400)),
        )
        .unwrap();
    let out = reap_with_authority(&opts, None, authority).unwrap();
    let row = out
        .lines()
        .find(|line| line.contains("\"kind\":\"run-dir\""))
        .unwrap();
    let result = reap_with_authority(&opts, Some(("D-123", row)), authority).unwrap();
    assert!(result.contains("quarantined") && !run.exists(), "{result}");
}

#[test]
fn deletion_needs_its_own_plan_and_audits_bytes() {
    use pg_worker_core::reaper::reap_with_authority;
    let f = Fixture::new();
    let tree = f.root.join("topic");
    age(&tree, 8 * 86_400);
    let opts = Options {
        repo: &f.repo,
        roots: std::slice::from_ref(&f.root),
        targets: &[],
        remote_targets: &[],
        idle_minutes: 60,
        state: &f.state,
        pins: &f.state.join("pins.json"),
        claims_reader: None,
        apply: false,
        include_merged: false,
        delete_remote: false,
    };
    let report = reap_with_authority(&opts, None, authority).unwrap();
    let stage = report
        .lines()
        .find(|l| l.contains("would-quarantine"))
        .unwrap();
    reap_with_authority(&opts, Some(("D-101", stage)), authority).unwrap();
    let slot = fs::read_dir(f.state.join("reap-quarantine"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let receipt = slot.join("receipt.json");
    let mut data: serde_json::Value = serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    data["quarantined_at"] = (now - 8 * 86_400).into();
    fs::write(&receipt, data.to_string()).unwrap();

    let report = reap_with_authority(&opts, None, authority).unwrap();
    let deletion = report.lines().find(|l| l.contains("would-delete")).unwrap();
    assert!(slot.exists());
    let out = reap_with_authority(&opts, Some(("D-101", stage)), authority).unwrap();
    assert!(
        out.contains("outside approved plan") && slot.exists(),
        "{out}"
    );
    let report = reap_with_authority(&opts, Some(("D-102", deletion)), |o, branch| {
        let (mut protection, pulls) = authority(o, branch)?;
        protection.claims.paths.push(tree.clone());
        Ok((protection, pulls))
    })
    .unwrap();
    assert!(
        report.contains("original path is in use") && slot.exists(),
        "{report}"
    );
    let report = reap_with_authority(&opts, Some(("D-102", deletion)), authority).unwrap();
    assert!(report.contains("deleted") && !slot.exists(), "{report}");
    let journal = fs::read_to_string(f.state.join("reap.ndjson")).unwrap();
    let row: serde_json::Value = serde_json::from_str(journal.lines().last().unwrap()).unwrap();
    assert_eq!(row["event"], "deleted");
    assert_eq!(row["approval"], "D-102");
    assert_eq!(row["entry"]["original"], tree.to_str().unwrap());
    assert!(row["entry"]["bytes"].as_u64().unwrap() > 0);
    assert!(git(&f.repo, &["branch", "--list", "topic"]).contains("topic"));
}

#[test]
fn cli_reports_without_creating_quarantine_and_refuses_incomplete_approval() {
    use std::process::Command;
    let f = Fixture::new();
    age(&f.root.join("topic"), 8 * 86_400);
    let command = || {
        let mut command = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"));
        command
            .arg("--reap")
            .arg("--repo")
            .arg(&f.repo)
            .arg("--root")
            .arg(&f.root)
            .arg("--state")
            .arg(&f.state)
            .arg("--pins")
            .arg(f.state.join("pins.json"))
            .env_remove("PG_SWEEP_CLAIMS_READER");
        command
    };
    let report = command().output().unwrap();
    assert!(report.status.success(), "{:?}", report);
    assert!(String::from_utf8_lossy(&report.stdout).contains("claims reader unavailable"));
    assert!(!f.state.join("reap-quarantine").exists());
    let out = command()
        .args(["--dry-run", "--approval", "D-123", "--plan", "missing"])
        .output()
        .unwrap();
    assert!(String::from_utf8_lossy(&out.stderr).contains("dry run cannot consume approval"));
    for flags in [&["--approval", "D-123"][..], &["--apply"][..]] {
        assert!(!command().args(flags).output().unwrap().status.success());
        assert!(f.root.join("topic").exists());
    }
}

#[path = "reaper/evidence.rs"]
mod evidence;
