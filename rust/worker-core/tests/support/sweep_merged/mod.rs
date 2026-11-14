//! Temporary Git repositories and a bounded GitHub CLI stub for sweep tests.
use pg_worker_core::host_command;
use serde_json::{json, Value};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Command, Output},
    time::Duration,
};

pub(super) fn git(repo: &Path, args: &[&str]) -> String {
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
pub(super) struct Fixture {
    _tmp: tempfile::TempDir,
    pub(super) home: PathBuf,
    pub(super) repo: PathBuf,
    pub(super) root: PathBuf,
    pub(super) units: PathBuf,
    pub(super) bin: PathBuf,
    pub(super) gh: PathBuf,
}
impl Fixture {
    pub(super) fn new() -> Self {
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
    pub(super) fn command(&self) -> Command {
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
    pub(super) fn run(&self, args: &[&str]) -> Value {
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
