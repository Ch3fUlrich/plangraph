//! A bare fixture remote with lane heads, a clone that runs the repository's pre-push hook,
//! and a fake `gh` that serves canned JSON and logs closes.
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

pub struct Fixture {
    pub root: tempfile::TempDir,
    pub work: PathBuf,
    pub state: PathBuf,
    pub gh: PathBuf,
    pub bin: PathBuf,
}

pub fn git(dir: &Path, args: &[&str]) -> String {
    let out = Command::new("git")
        .current_dir(dir)
        .args([
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
        ])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().to_string()
}

impl Fixture {
    pub fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let base = root.path().canonicalize().unwrap();
        let bare = base.join("fixture/origin.git");
        fs::create_dir_all(&bare).unwrap();
        git(&bare, &["init", "--bare", "-b", "main"]);
        let work = base.join("work");
        git(
            &base,
            &["clone", bare.to_str().unwrap(), work.to_str().unwrap()],
        );
        git(&work, &["checkout", "-b", "main"]);
        fs::write(work.join("base.txt"), "base\n").unwrap();
        git(&work, &["add", "."]);
        git(&work, &["commit", "-m", "base"]);
        git(&work, &["push", "origin", "main"]);
        let gh = base.join("gh");
        let bin = base.join("bin");
        fs::create_dir_all(&gh).unwrap();
        fs::create_dir_all(&bin).unwrap();
        let script = bin.join("gh");
        fs::write(
            &script,
            r#"#!/bin/sh
d="$FAKE_GH_DIR"
case "$1 $2" in
  "pr list") cat "$d/pulls.json" ;;
  "pr view") cat "$d/pr-$3.json" ;;
  "run view") cat "$d/run-$3.json" ;;
  "pr close") echo "$3" >> "$d/closed.log" ;;
  *) echo "unexpected gh $*" >&2; exit 9 ;;
esac
"#,
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
        }
        fs::write(gh.join("pulls.json"), "[]").unwrap();
        fs::write(gh.join("claims.json"), r#"{"paths":[],"branches":[]}"#).unwrap();
        let state = base.join("state");
        Self {
            root,
            work,
            state,
            gh,
            bin,
        }
    }

    /// Commit one file on a new lane branch (from main, or from `from`) and push it.
    pub fn lane(&self, name: &str, from: &str, file: &str, text: &str) -> String {
        git(&self.work, &["checkout", "-q", "-B", name, from]);
        fs::write(self.work.join(file), text).unwrap();
        git(&self.work, &["add", "."]);
        git(&self.work, &["commit", "-m", &format!("{name} {file}")]);
        git(
            &self.work,
            &[
                "push",
                "-q",
                "-f",
                "origin",
                &format!("{name}:refs/heads/{name}"),
            ],
        );
        git(&self.work, &["rev-parse", "HEAD"])
    }

    pub fn main_ff(&self, lane: &str) {
        git(&self.work, &["checkout", "-q", "main"]);
        git(&self.work, &["merge", "--ff-only", lane]);
        git(&self.work, &["push", "-q", "origin", "main"]);
    }

    pub fn main_pick(&self, sha: &str) {
        git(&self.work, &["checkout", "-q", "main"]);
        // Another main commit first: the picked commit must differ from the lane's own.
        fs::write(self.work.join("other.txt"), sha).unwrap();
        git(&self.work, &["add", "."]);
        git(&self.work, &["commit", "-m", "other"]);
        git(&self.work, &["cherry-pick", sha]);
        git(&self.work, &["push", "-q", "origin", "main"]);
    }

    /// From here on every push of the clone runs the repository's own pre-push hook.
    pub fn hook_on(&self, app_remote: bool) {
        let hooks = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/hooks");
        let hooks = hooks.canonicalize().unwrap();
        let config = |k: &str, v: &str| {
            Command::new("git")
                .current_dir(&self.work)
                .args(["config", k, v])
                .output()
                .unwrap();
        };
        config("core.hooksPath", hooks.to_str().unwrap());
        if app_remote {
            config("remote.origin.plangraph-lane-delete", "app");
        }
    }

    pub fn heads(&self) -> Vec<String> {
        let bare = self.work.parent().unwrap().join("fixture/origin.git");
        git(
            &bare,
            &[
                "for-each-ref",
                "--format=%(refname:strip=3)",
                "refs/heads/lane",
            ],
        )
        .lines()
        .map(String::from)
        .collect()
    }

    pub fn set(&self, name: &str, text: &str) {
        fs::write(self.gh.join(name), text).unwrap();
    }

    pub fn cli(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .env_clear()
            .env("HOME", self.root.path())
            .env("PATH", format!("{}:/usr/bin:/bin", self.bin.display()))
            .env("FAKE_GH_DIR", &self.gh)
            .args(args)
            .output()
            .unwrap()
    }

    /// `lane-cleanup` over the fixture; the claims reader is the canned claims file.
    pub fn cleanup(&self, extra: &[&str]) -> (i32, String) {
        let reader = format!("cat {}", self.gh.join("claims.json").display());
        let work = self.work.to_str().unwrap();
        let state = self.state.to_str().unwrap();
        let mut args = vec![
            "lane-cleanup",
            "--repo",
            work,
            "--state",
            state,
            "--gh-repo",
            "fixture/origin",
            "--claims-reader",
            &reader,
        ];
        args.extend_from_slice(extra);
        let out = self.cli(&args);
        let text = format!(
            "{}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
        (out.status.code().unwrap(), text)
    }

    pub fn record_replacement(&self, key: &str, old: &str, new: &str) -> Output {
        self.cli(&[
            "lane-cleanup",
            "record-replacement",
            "--repo",
            self.work.to_str().unwrap(),
            "--state",
            self.state.to_str().unwrap(),
            "--batch-key",
            key,
            "--old",
            old,
            "--new",
            new,
        ])
    }

    pub fn restore_text(&self) -> String {
        let dir = self.state.join("sweep");
        let mut text = String::new();
        if let Ok(entries) = fs::read_dir(dir) {
            for e in entries {
                text.push_str(&fs::read_to_string(e.unwrap().path()).unwrap());
            }
        }
        text
    }
}
