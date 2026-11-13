#![cfg(unix)]
use pg_worker_core::{host_command, prod_paths};
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Output},
    time::Duration,
};

fn git(repo: &Path, args: &[&str]) -> String {
    String::from_utf8(
        host_command::output(
            Command::new("git")
                .arg("-C")
                .arg(repo)
                .args([
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                ])
                .args(args),
            Duration::from_secs(10),
        )
        .unwrap(),
    )
    .unwrap()
}
struct Fixture {
    _tmp: tempfile::TempDir,
    home: PathBuf,
    repo: PathBuf,
    root: PathBuf,
    units: PathBuf,
    bin: PathBuf,
    gh: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().to_path_buf();
        let repo = home.join("main");
        let origin = home.join("origin.git");
        let root = home.join("trees");
        let units = home.join("units");
        let bin = home.join("bin");
        let gh = home.join("gh");
        for p in [&repo, &origin, &root, &units, &bin, &gh] {
            fs::create_dir(p).unwrap();
        }
        git(&origin, &["init", "--bare", "-q", "--initial-branch=main"]);
        git(&repo, &["init", "-qb", "main"]);
        fs::write(repo.join(".gitignore"), "target/\nnode_modules/\n.env\n").unwrap();
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-qm", "initial"]);
        git(
            &repo,
            &["remote", "add", "origin", origin.to_str().unwrap()],
        );
        git(&repo, &["push", "-q", "origin", "main"]);
        git(
            &repo,
            &[
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/main",
            ],
        );
        let mut merged = vec![];
        for (index, name) in [
            "merged",
            "closed",
            "closed-contained",
            "open",
            "dirty",
            "unpushed",
            "unit",
            "ignored",
            "phase/open",
            "unattached",
            "gone",
            "master",
            "cwd",
        ]
        .iter()
        .enumerate()
        {
            let tree = root.join(name);
            git(
                &repo,
                &["worktree", "add", "-qb", name, tree.to_str().unwrap()],
            );
            if *name == "closed" {
                fs::write(tree.join("local"), "not on main").unwrap();
                git(&tree, &["add", "."]);
                git(&tree, &["commit", "-qm", "closed unmerged"]);
            }
            if *name != "gone" {
                git(&repo, &["push", "-q", "origin", name]);
            }
            if *name == "unpushed" {
                fs::write(tree.join("local"), "not pushed").unwrap();
                git(&tree, &["add", "."]);
                git(&tree, &["commit", "-qm", "unpublished"]);
            }
            let sha = git(&tree, &["rev-parse", "HEAD"]).trim().to_string();
            let state = if name.contains("open") {
                "OPEN"
            } else if name.starts_with("closed") {
                "CLOSED"
            } else {
                "MERGED"
            };
            let pr = json!({"state": state, "headRefName": name, "headRefOid": sha, "mergedAt": if state == "MERGED" { Some("2026-11-13T00:00:00Z") } else { None }, "number": index + 1});
            if *name != "gone" {
                let file = gh.join(format!("{name}.json"));
                fs::create_dir_all(file.parent().unwrap()).unwrap();
                fs::write(file, json!([pr.clone()]).to_string()).unwrap();
            } else {
                fs::write(gh.join("gone.json"), "[]").unwrap();
            }
            if state == "MERGED" && *name != "gone" {
                merged.push(pr);
            }
        }
        fs::write(gh.join("merged-batch.json"), json!(merged).to_string()).unwrap();
        fs::write(bin.join("gh"), "#!/bin/sh\necho call >> \"$FIXTURE_GH/calls\"\nhead=\nstate=\nwhile [ $# -gt 0 ]; do\n case \"$1\" in --head) head=$2; shift;; --state) state=$2; shift;; esac\n shift\ndone\nif [ \"$state\" = merged ]; then cat \"$FIXTURE_GH/merged-batch.json\"; else cat \"$FIXTURE_GH/$head.json\"; fi\n").unwrap();
        fs::set_permissions(bin.join("gh"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(root.join("dirty/untracked"), "valuable").unwrap();
        fs::write(root.join("ignored/.env"), "secret").unwrap();
        fs::write(
            units.join("keep.service"),
            format!(
                "[Service]\nExecStart=/usr/bin/true\nWorkingDirectory={}\n",
                root.join("unit").display()
            ),
        )
        .unwrap();
        git(
            &repo,
            &[
                "worktree",
                "remove",
                root.join("unattached").to_str().unwrap(),
            ],
        );
        Self {
            _tmp: tmp,
            home,
            repo,
            root,
            units,
            bin,
            gh,
        }
    }
    fn command(&self) -> Command {
        let mut c = Command::new(env!("CARGO_BIN_EXE_pg-disk-hygiene"));
        c.args(["sweep-merged", "--repo"])
            .arg(&self.repo)
            .arg("--root")
            .arg(&self.root)
            .arg("--unit-dir")
            .arg(&self.units)
            .arg("--state")
            .arg(self.home.join("state"))
            .env("HOME", &self.home)
            .env("XDG_CONFIG_HOME", self.home.join("xdg"))
            .env("PATH", format!("{}:/usr/bin:/bin", self.bin.display()))
            .env("FIXTURE_GH", &self.gh)
            .env("GIT_DIR", "/poison")
            .env("GIT_WORK_TREE", "/poison")
            .env("GIT_INDEX_FILE", "/poison")
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "core.bare")
            .env("GIT_CONFIG_VALUE_0", "true");
        c
    }
    fn run(&self, args: &[&str]) -> Value {
        let out = self.command().arg("--json").args(args).output().unwrap();
        decode(out)
    }
}
fn decode(out: Output) -> Value {
    assert!(
        out.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_slice(&out.stdout).unwrap()
}
fn names(report: &Value) -> Vec<&str> {
    report["actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["branch"].as_str().unwrap())
        .collect()
}

fn proc_blocked(report: &Value) -> bool {
    report["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .any(|s| s.as_str().unwrap().contains("Permission denied"))
}

#[test]
fn sweep_merged_dry_run_plan_preserves_guards_and_bounds_calls() {
    let f = Fixture::new();
    // A process in a worktree blocks its removal.
    let mut child = Command::new("sleep")
        .arg("60")
        .current_dir(f.root.join("cwd"))
        .spawn()
        .unwrap();
    let report = f.run(&["--remote"]);
    child.kill().unwrap();
    child.wait().unwrap();
    let candidates = names(&report);
    if proc_blocked(&report) {
        assert_eq!(candidates, ["unattached"]);
        assert!(f.root.join("merged").exists());
        println!("/proc is unreadable: verified fail-closed worktree guards; run with full /proc visibility for removal coverage");
        return;
    }
    assert!(
        candidates.contains(&"merged")
            && candidates.contains(&"closed-contained")
            && candidates.contains(&"unattached")
            && candidates.contains(&"gone"),
        "{report}"
    );
    for name in [
        "closed",
        "open",
        "phase/open",
        "dirty",
        "unpushed",
        "unit",
        "ignored",
        "master",
        "cwd",
    ] {
        assert!(!candidates.contains(&name), "unexpected {name}: {report}");
    }
    assert!(report["dry_run"].as_bool().unwrap());
    assert!(!f.home.join("state").exists());
    assert!(f.root.join("merged").exists());
    let calls = fs::read_to_string(f.gh.join("calls"))
        .unwrap()
        .lines()
        .count();
    assert_eq!(calls, report["gh_calls"].as_u64().unwrap() as usize);
    assert!(calls <= 64);
    assert_eq!(names(&f.run(&["--max", "1"])).len(), 1);
    let out = f.command().args(["--max", "1"]).output().unwrap();
    assert!(out.status.success());
    let plan = String::from_utf8(out.stdout).unwrap();
    assert!(plan.contains("would-remove"));
    println!("temp fixture dry-run plan:\n{plan}");
}

#[test]
fn sweep_merged_apply_journals_and_remote_is_opt_in() {
    let f = Fixture::new();
    let report = f.run(&["--apply"]);
    let journal = fs::read_to_string(report["restore"].as_str().unwrap()).unwrap();
    assert!(journal.starts_with("branch\tsha\tpr\tworktree\n"));
    if proc_blocked(&report) {
        assert_eq!(names(&report), ["unattached"]);
        assert!(f.root.join("merged").exists());
        assert!(journal.contains("unattached\t"));
        return;
    }
    assert!(journal.contains("merged\t"));
    assert!(!f.root.join("merged").exists());
    let heads = git(
        &f.repo,
        &["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
    );
    assert!(!heads.lines().any(|b| b == "merged"));
    assert!(heads.lines().any(|b| b == "gone"));
    assert!(!git(&f.repo, &["ls-remote", "--heads", "origin", "merged"]).is_empty());
    for name in [
        "closed",
        "open",
        "dirty",
        "unpushed",
        "unit",
        "ignored",
        "phase/open",
    ] {
        assert!(f.root.join(name).exists(), "{name}");
    }
    // A second fixture exercises explicit remote deletion and the SHA lease.
    let remote = Fixture::new();
    let report = remote.run(&["--apply", "--remote"]);
    assert!(names(&report).contains(&"merged"));
    assert!(git(&remote.repo, &["ls-remote", "--heads", "origin", "merged"]).is_empty());
}

#[test]
fn sweep_merged_failures_and_truncated_history_fail_closed() {
    let f = Fixture::new();
    fs::write(f.gh.join("merged.json"), json!(vec![json!({"state":"OPEN", "headRefName":"merged", "headRefOid":"deadbeef", "mergedAt":null, "number":99}); 100]).to_string()).unwrap();
    assert!(!names(&f.run(&[])).contains(&"merged"));
    fs::write(f.gh.join("merged-batch.json"), "not-json").unwrap();
    let out = f.command().arg("--apply").output().unwrap();
    assert!(!out.status.success());
    assert!(f.root.join("merged").exists());
    assert!(!f.home.join("state/sweep").exists());
    // Helper commands sanitize Git environment and stay inside fixture repos.
    assert!(prod_paths::command_output(
        Command::new("git")
            .arg("-C")
            .arg(&f.repo)
            .args(["status", "--porcelain"])
    )
    .is_ok());
}

#[test]
fn sweep_merged_enforces_gh_call_bound() {
    let f = Fixture::new();
    for i in 0..70 {
        let branch = format!("aaa-{i:03}");
        git(&f.repo, &["branch", &branch, "main"]);
        fs::write(f.gh.join(format!("{branch}.json")), "[]").unwrap();
    }
    let report = f.run(&[]);
    assert_eq!(report["gh_calls"], 64);
    assert!(report["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v == "gh call bound reached"));
    assert!(names(&report).is_empty());
}

#[test]
fn sweep_merged_journal_precedes_branch_deletion_and_failure_keeps_restore() {
    let f = Fixture::new();
    // Observe (and reject) the first local branch deletion. Its worktree must
    // already be absent and its exact tip must already be in the durable TSV.
    fs::write(f.bin.join("git"), "#!/bin/sh\ncase \" $* \" in\n *' branch -D '*)\n  for last do :; done\n  test ! -e \"$FIXTURE_ROOT/$last\" || exit 91\n  sha=$(/usr/bin/git -C \"$FIXTURE_REPO\" rev-parse \"refs/heads/$last\") || exit 92\n  grep -F \"$sha\" \"$FIXTURE_STATE\"/sweep/*.tsv > \"$FIXTURE_GH/journal-observed\" || exit 93\n  exit 94;;\nesac\nexec /usr/bin/git \"$@\"\n").unwrap();
    fs::set_permissions(f.bin.join("git"), fs::Permissions::from_mode(0o755)).unwrap();
    let output = f
        .command()
        .args(["--apply", "--max", "1"])
        .env("FIXTURE_ROOT", &f.root)
        .env("FIXTURE_REPO", &f.repo)
        .env("FIXTURE_STATE", f.home.join("state"))
        .output()
        .unwrap();
    assert!(!output.status.success());
    let observed = fs::read_to_string(f.gh.join("journal-observed")).unwrap();
    assert!(!observed.is_empty());
    let branches = git(
        &f.repo,
        &["for-each-ref", "--format=%(refname:short)", "refs/heads/"],
    );
    assert!(branches
        .lines()
        .any(|b| observed.starts_with(&format!("{b}\t"))));
    assert_eq!(fs::read_dir(f.home.join("state/sweep")).unwrap().count(), 1);
}

#[test]
fn sweep_merged_protects_default_and_outside_root_worktree() {
    let f = Fixture::new();
    git(
        &f.repo,
        &[
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/merged",
        ],
    );
    // A matching merged PR cannot authorize deletion of a tree outside --root.
    let outside = f.home.join("outside");
    git(
        &f.repo,
        &[
            "worktree",
            "move",
            f.root.join("closed-contained").to_str().unwrap(),
            outside.to_str().unwrap(),
        ],
    );
    let report = f.run(&[]);
    assert!(!names(&report).contains(&"merged"));
    assert!(!names(&report).contains(&"closed-contained"));
    assert!(outside.exists());
}
