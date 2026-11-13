//! Exercise the shipped library with hostile caller configuration in a subprocess.
use pg_ctx::mirror::Mirror;
use std::{fs, path::Path, process::Command};

fn fixture_git(dir: &Path, args: &[&str]) -> String {
    let output = pg_worker_core::workspace_resolver::git_command(dir)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", dir.join("absent-config"))
        .env("GIT_AUTHOR_NAME", "Mirror Test")
        .env("GIT_COMMITTER_NAME", "Mirror Test")
        .env("GIT_AUTHOR_EMAIL", "mirror@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "mirror@example.invalid")
        .output()
        .unwrap();
    assert!(output.status.success(), "Git fixture failed");
    String::from_utf8(output.stdout).unwrap().trim().into()
}

fn fixture() -> tempfile::TempDir {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    let origin = root.join("origin.git");
    fs::create_dir(&origin).unwrap();
    fixture_git(&origin, &["init", "--bare", "--initial-branch=main"]);
    let tree = fixture_git(&origin, &["mktree"]);
    let head = fixture_git(&origin, &["commit-tree", &tree, "-m", "fixture"]);
    fixture_git(&origin, &["update-ref", "refs/heads/main", &head]);
    fs::write(root.join("head"), &head).unwrap();
    fs::write(
        root.join("repos.json"),
        serde_json::to_vec(&serde_json::json!({"demo": origin})).unwrap(),
    )
    .unwrap();
    tmp
}

#[test]
fn mirror_ignores_caller_git_configuration() {
    let tmp = fixture();
    let root = tmp.path();
    let home = root.join("home");
    let xdg = root.join("xdg");
    fs::create_dir(&home).unwrap();
    fs::create_dir_all(xdg.join("git")).unwrap();
    let hostile = "[protocol \"file\"]\n\tallow = never\n\
        [url \"/missing-mirror-remote/\"]\n\tinsteadOf = /\n";
    fs::write(home.join(".gitconfig"), hostile).unwrap();
    fs::write(xdg.join("git/config"), hostile).unwrap();
    let completion = root.join("exercised");
    // Require fresh evidence even if fixture construction ever starts reusing roots.
    fs::write(&completion, "stale completion").unwrap();
    fs::remove_file(&completion).unwrap();
    let output = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "mirror_configuration_child", "--nocapture"])
        .env("PG_CTX_CONFIG_FIXTURE", root)
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", &xdg)
        .env("GIT_CONFIG_GLOBAL", home.join(".gitconfig"))
        .env("GIT_CONFIG_NOSYSTEM", "0")
        .env("GIT_DIR", root.join("not-a-repository"))
        .env("GIT_COMMON_DIR", root.join("not-a-common-dir"))
        .env("GIT_WORK_TREE", root.join("not-a-worktree"))
        .env("GIT_OBJECT_DIRECTORY", root.join("not-an-object-dir"))
        .env("GIT_INDEX_FILE", root.join("not-an-index"))
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "protocol.file.allow")
        .env("GIT_CONFIG_VALUE_0", "never")
        // The fixture is already built: mirror clone/fetch must not find git
        // or git-upload-pack, even indirectly through gix's file transport.
        .env("PATH", root.join("no-executables"))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read(root.join("exercised")).unwrap(),
        b"clone and fetch"
    );
}

#[test]
fn mirror_configuration_child() {
    let owned;
    let root = match std::env::var_os("PG_CTX_CONFIG_FIXTURE") {
        Some(root) => std::path::PathBuf::from(root),
        None => {
            // Exercise clone/fetch directly, without running/spawning the parent again.
            owned = fixture();
            owned.path().to_owned()
        }
    };
    let root = root.as_path();
    let mirror = Mirror::from_repos(&root.join("repos.json"), &root.join("mirrors")).unwrap();
    let expected = fs::read_to_string(root.join("head")).unwrap();
    assert_eq!(mirror.fetch("demo").unwrap().main_head, expected);
    assert_eq!(mirror.fetch("demo").unwrap().main_head, expected);
    fs::write(root.join("exercised"), "clone and fetch").unwrap();
}
