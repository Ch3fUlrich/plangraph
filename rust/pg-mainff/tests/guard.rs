use pg_mainff::{
    advance,
    process::{Error, Output, Runner, System},
};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
struct Fixture {
    _temp: tempfile::TempDir,
    repo: PathBuf,
    state: PathBuf,
    loaded: String,
}
fn git(repo: &Path, args: &[&str]) -> String {
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
fn write(repo: &Path, name: &str, source: &str) {
    let path = repo.join(name);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, source).unwrap();
}
impl Fixture {
    fn new() -> Self {
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
    fn target(&self, path: &str, text: &str) -> String {
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
    fn run(&self) -> (i32, String) {
        advance::main_ff(
            &System,
            &self.repo,
            &self.state,
            false,
            "plangraph-keeper.service",
        )
    }
    fn head(&self) -> String {
        git(&self.repo, &["rev-parse", "HEAD"])
    }
}
#[test]
fn ff_checks_keeper_import_closure() {
    for (path, held) in [
        ("src/plangraph/keeper/daemon.py", true),
        ("src/plangraph/shared.py", true),
        ("src/plangraph/nested.py", true),
        ("tools/keeper_helper.py", true),
        ("src/plangraph/keeper/unused_new.py", false),
        ("src/plangraph/spawner/unused.py", false),
    ] {
        let f = Fixture::new();
        let target = f.target(path, "VALUE = 2\n");
        let (code, line) = f.run();
        assert_eq!(code, if held { 3 } else { 0 }, "{line}");
        assert_eq!(
            f.head(),
            if held {
                f.loaded.clone()
            } else {
                target.clone()
            }
        );
        assert!(fs::read_to_string(&f.state).unwrap().contains(&f.loaded));
        if held {
            assert!(line.starts_with("HELD keeper code changed: "));
            assert!(line.contains(path));
            assert!(line.contains(&format!("loaded {} target {target}", f.loaded)));
        }
    }
}
#[test]
fn unreadable_loaded_state_holds_instead_of_assuming_stopped() {
    let f = Fixture::new();
    f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
    fs::write(&f.state, "{").unwrap();
    assert_eq!(f.run().0, 3);
    assert_eq!(f.head(), f.loaded);
}
#[test]
fn no_keeper_state_allows_move() {
    for state in [None, Some(i32::MAX as u32 - 1)] {
        let f = Fixture::new();
        let target = f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
        match state {
            None => fs::remove_file(&f.state).unwrap(),
            Some(pid) => fs::write(
                &f.state,
                serde_json::json!({"pid": pid, "loaded_sha": f.loaded}).to_string(),
            )
            .unwrap(),
        }
        let (code, line) = f.run();
        assert_eq!(code, 0, "{state:?}: {line}");
        assert_eq!(f.head(), target);
    }
}
#[test]
fn detached_and_dirty_are_refused() {
    let f = Fixture::new();
    f.target("src/plangraph/spawner/unused.py", "VALUE = 2\n");
    git(&f.repo, &["checkout", "--detach"]);
    assert_eq!(f.run(), (2, "REFUSED branch HEAD, want main".into()));
    git(&f.repo, &["checkout", "main"]);
    write(&f.repo, "src/plangraph/shared.py", "dirty\n");
    assert_eq!(f.run().0, 2);
    assert_eq!(f.head(), f.loaded);
}
struct FailGit<'a> {
    verb: &'a str,
}
impl Runner for FailGit<'_> {
    fn run(&self, args: &[&str]) -> Result<Output, Error> {
        if args[0] == "git" && args[3] == self.verb && args.get(4) != Some(&"--absolute-git-dir") {
            return Err(Error::Unavailable);
        }
        if args[0] == "git" && args[3] == "fetch" {
            return Ok(Output {
                code: 0,
                out: String::new(),
                err: String::new(),
            });
        }
        System.run(args)
    }
}
#[test]
fn git_failures_refresh_status_with_named_cause() {
    for verb in [
        "symbolic-ref",
        "status",
        "fetch",
        "rev-parse",
        "merge-base",
        "merge",
    ] {
        let f = Fixture::new();
        f.target("src/plangraph/spawner/unused.py", "VALUE = 2\n");
        let status = advance::status_path(&System, &f.repo).unwrap();
        fs::write(&status, "HELD stale cause\n").unwrap();
        let (code, line) = advance::main_ff(
            &FailGit { verb },
            &f.repo,
            &f.state,
            true,
            "plangraph-keeper.service",
        );
        assert_eq!(code, 2);
        assert_eq!(line, format!("REFUSED git {verb} failed"));
        assert_eq!(fs::read_to_string(status).unwrap(), line + "\n");
        assert_eq!(f.head(), f.loaded);
    }
}
#[test]
fn literal_dynamic_and_parent_package_imports_are_watched() {
    for source in [
        "def later():\n    import importlib\n    return importlib.import_module('plangraph.nested')\n",
        "def later():\n    return __import__('plangraph.nested')\n",
        "from importlib import import_module\ndef later():\n    return import_module('.nested', package='plangraph')\n",
    ] {
        let mut f=Fixture::new(); write(&f.repo,"src/plangraph/keeper/daemon.py",source);
        git(&f.repo,&["add","."]); git(&f.repo,&["commit","-m","dynamic imports"]); f.loaded=git(&f.repo,&["rev-parse","HEAD"]);
        fs::write(&f.state,serde_json::json!({"pid":std::process::id(),"loaded_sha":f.loaded}).to_string()).unwrap();
        f.target("src/plangraph/nested.py","VALUE = 2\n"); assert_eq!(f.run().0,3);
    }
    let f = Fixture::new();
    f.target("src/plangraph/__init__.py", "VALUE = 2\n");
    assert_eq!(f.run().0, 3);
}
#[test]
fn invalid_import_source_holds() {
    let f = Fixture::new();
    f.target("src/plangraph/shared.py", "def broken(:\n");
    assert_eq!(f.run().0, 3);
    assert_eq!(f.head(), f.loaded);
}
#[test]
fn default_command_fetches_and_advances_from_fixture_origin() {
    let f = Fixture::new();
    let target = f.target("src/plangraph/spawner/unused.py", "VALUE = 2\n");
    let origin = f._temp.path().join("origin.git");
    git(
        f._temp.path(),
        &["init", "--bare", origin.to_str().unwrap()],
    );
    // Populate the fixture origin locally; never invoke git push.
    git(
        &origin,
        &[
            "fetch",
            f.repo.to_str().unwrap(),
            &format!("{target}:refs/heads/main"),
        ],
    );
    git(
        &f.repo,
        &["remote", "add", "origin", origin.to_str().unwrap()],
    );
    git(
        &f.repo,
        &["update-ref", "refs/remotes/origin/main", &f.loaded],
    );
    let result = Command::new(env!("CARGO_BIN_EXE_pg-mainff"))
        .args([
            "main-ff",
            "--repo",
            f.repo.to_str().unwrap(),
            "--keeper-state",
            f.state.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains(&format!("UPDATED main {target}")));
    assert_eq!(f.head(), target);
}

#[test]
fn python_compatibility_command_uses_rust_guard() {
    let f = Fixture::new();
    f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let result=Command::new("python3").arg("-c").arg("from plangraph.fleet_sync.__main__ import main; import sys; sys.exit(main(sys.argv[1:]))")
        .args(["main-ff","--repo",f.repo.to_str().unwrap(),"--keeper-state",f.state.to_str().unwrap(),"--no-fetch"])
        .env("PYTHONPATH",root.join("src")).output().unwrap();
    assert_eq!(
        result.status.code(),
        Some(3),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(String::from_utf8_lossy(&result.stdout).contains("HELD keeper code changed:"));
    assert_eq!(f.head(), f.loaded);
}
