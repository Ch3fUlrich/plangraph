//! Reproduce a pre-push caller without ever using the real repository as a victim.
use pg_worker_core::{git, git_test_support, workspace_resolver};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Command,
};

fn fixture_git(root: &Path, args: &[&str]) -> String {
    let output = git_test_support::command(root).args(args).output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().into()
}

fn snapshot(root: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    fn visit(root: &Path, dir: &Path, result: &mut BTreeMap<PathBuf, Vec<u8>>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(root, &path, result);
            } else {
                result.insert(
                    path.strip_prefix(root).unwrap().into(),
                    fs::read(path).unwrap(),
                );
            }
        }
    }
    let mut result = BTreeMap::new();
    visit(root, root, &mut result);
    result
}

#[test]
fn inherited_hook_context_cannot_mutate_another_repository() {
    let tmp = tempfile::tempdir().unwrap();
    let victim = tmp.path().join("victim");
    fs::create_dir(&victim).unwrap();
    fixture_git(&victim, &["init", "-q", "-b", "main"]);
    fixture_git(
        &victim,
        &["commit", "--allow-empty", "-qm", "victim baseline"],
    );
    let before = snapshot(&victim);
    let global = tmp.path().join("host.gitconfig");
    fs::write(&global, "[core]\n bare = true\n[user]\n name = hostile\n").unwrap();
    for api in ["basic", "resolver", "preconfigured"] {
        let fixture = tmp.path().join(api);
        fs::create_dir(&fixture).unwrap();
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "fixture_process", "--nocapture"])
            .env("PLANGRAPH_TEST_GIT_FIXTURE_CHILD", &fixture)
            .env("PLANGRAPH_TEST_GIT_FIXTURE_API", api)
            .env("GIT_DIR", victim.join(".git"))
            .env("GIT_COMMON_DIR", victim.join(".git"))
            .env("GIT_WORK_TREE", &victim)
            .env("GIT_INDEX_FILE", victim.join(".git/index"))
            .env("GIT_OBJECT_DIRECTORY", victim.join(".git/objects"))
            .env("GIT_CONFIG_GLOBAL", &global)
            .env("GIT_CONFIG_COUNT", "1")
            .env("GIT_CONFIG_KEY_0", "core.bare")
            .env("GIT_CONFIG_VALUE_0", "true")
            .env("GIT_CONFIG_PARAMETERS", "invalid injected parameters")
            .env("GIT_FUTURE_HOOK_SENTINEL", "must be removed")
            .output()
            .unwrap();
        assert_eq!(
            snapshot(&victim),
            before,
            "{api} mutated the fake victim config, refs or index"
        );
        assert!(
            output.status.success(),
            "{api}: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(fixture.join(".git/config").is_file());
    }
}

#[test]
fn fixture_process() {
    let Some(root) = std::env::var_os("PLANGRAPH_TEST_GIT_FIXTURE_CHILD") else {
        return;
    };
    let root = PathBuf::from(root);
    let api = std::env::var("PLANGRAPH_TEST_GIT_FIXTURE_API").unwrap();
    let run = |args: &[&str]| {
        if api == "basic" {
            return git::git(&root, args).unwrap();
        }
        let mut command = workspace_resolver::git_command(&root);
        if api == "preconfigured" {
            command
                .env("GIT_DIR", std::env::var_os("GIT_DIR").unwrap())
                .env("GIT_CONFIG_KEY_8", "injected");
            git_test_support::configure(&mut command, &root);
        }
        let vars: BTreeMap<_, _> = command.get_envs().collect();
        for key in [
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_PARAMETERS",
            "GIT_FUTURE_HOOK_SENTINEL",
        ] {
            assert_eq!(vars[std::ffi::OsStr::new(key)], None, "{key}");
        }
        assert_eq!(vars[std::ffi::OsStr::new("HOME")], Some(root.as_os_str()));
        assert_eq!(
            vars[std::ffi::OsStr::new("XDG_CONFIG_HOME")],
            Some(root.join(".config").as_os_str())
        );
        let output = command.args(args).output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().into()
    };
    run(&["init", "-q", "-b", "main"]);
    run(&["config", "user.name", "fixture only"]);
    fs::write(root.join("own.txt"), "fixture data\n").unwrap();
    run(&["add", "own.txt"]);
    run(&["commit", "-qm", "fixture commit"]);
    assert!(!run(&["rev-parse", "HEAD"]).is_empty());
}
