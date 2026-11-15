use pg_mainff::{advance, process::System};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
pub struct Fixture {
    pub _temp: tempfile::TempDir,
    pub repo: PathBuf,
    pub state: PathBuf,
    pub loaded: String,
}
pub fn git(repo: &Path, args: &[&str]) -> String {
    let mut command = Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    let out = command
        .arg("-C")
        .arg(repo)
        .args(args)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_AUTHOR_NAME", "Fixture")
        .env("GIT_COMMITTER_NAME", "Fixture")
        .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "git {args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}
pub fn write(repo: &Path, name: &str, source: &str) {
    let path = repo.join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, source).unwrap();
}
impl Fixture {
    pub fn new() -> Self {
        let temp = tempfile::tempdir().unwrap();
        let repo = temp.path().join("repo");
        fs::create_dir(&repo).unwrap();
        git(&repo, &["init", "-b", "main"]);
        for (path, text) in [
            ("src/plangraph/__init__.py", ""),
            ("src/plangraph/shared.py", "def later():\n    from . import nested\n    from tools import keeper_helper\n    return nested.VALUE\n"),
            ("tools/__init__.py", ""), ("tools/keeper_helper.py", "VALUE = 1\n"),
            ("src/plangraph/nested.py", "VALUE = 1\n"),
            ("src/plangraph/keeper/__init__.py", ""),
            ("src/plangraph/keeper/daemon.py", "from plangraph import shared\n"),
            ("src/plangraph/spawner/unused.py", "VALUE = 1\n"),
        ] { write(&repo, path, text); }
        git(&repo, &["add", "."]);
        git(&repo, &["commit", "-m", "loaded"]);
        let loaded = git(&repo, &["rev-parse", "HEAD"]);
        let state = temp.path().join("keeper-loaded.json");
        fs::write(
            &state,
            serde_json::json!({"pid": std::process::id(), "loaded_sha": loaded}).to_string(),
        )
        .unwrap();
        Self {
            _temp: temp,
            repo,
            state,
            loaded,
        }
    }
    pub fn target(&self, path: &str, text: &str) -> String {
        write(&self.repo, path, text);
        git(&self.repo, &["add", "."]);
        git(&self.repo, &["commit", "-m", "target"]);
        let target = git(&self.repo, &["rev-parse", "HEAD"]);
        git(
            &self.repo,
            &["update-ref", "refs/remotes/origin/main", &target],
        );
        git(&self.repo, &["reset", "--hard", &self.loaded]);
        target
    }
    pub fn run(&self) -> (i32, String) {
        advance::main_ff(
            &System,
            &self.repo,
            &self.state,
            false,
            "plangraph-keeper.service",
        )
    }
    pub fn head(&self) -> String {
        git(&self.repo, &["rev-parse", "HEAD"])
    }
}
