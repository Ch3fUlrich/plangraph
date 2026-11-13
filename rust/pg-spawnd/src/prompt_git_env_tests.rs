use super::*;
const CHILD: &str = "PG_PROMPT_FIXTURE_ISOLATION";
#[test]
fn inherited_git_context_cannot_redirect_fixtures() {
    if std::env::var_os(CHILD).is_some() {
        let (root, repo, diff) = fixture();
        let actual = git(&root, &["rev-parse", "--show-toplevel"]);
        assert_eq!(
            Path::new(&actual).canonicalize().unwrap(),
            root.canonicalize().unwrap()
        );
        let expected = fixture_git_command(&root)
            .args(["diff", "--binary", &diff.base_sha, &diff.head_sha])
            .output()
            .unwrap();
        assert!(expected.status.success());
        assert_eq!(read_diff(&repo, &diff, 262144).unwrap(), expected.stdout);
        std::fs::remove_dir_all(root).unwrap();
        return;
    }
    for variant in ["routing", "configuration"] {
        let temp = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        let redirected = temp.join("redirected");
        std::fs::create_dir_all(&redirected).unwrap();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap());
        for (key, _) in std::env::vars_os() {
            if key
                .to_string_lossy()
                .to_ascii_uppercase()
                .starts_with("GIT_")
            {
                child.env_remove(key);
            }
        }
        child
            .args([
                "--exact",
                "prompt_git::tests::env_isolation::inherited_git_context_cannot_redirect_fixtures",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .env("HOME", &temp)
            .env("XDG_CONFIG_HOME", temp.join("config"));
        if variant == "routing" {
            // This is an empty disposable directory, never a checkout's real .git.
            child
                .env("GIT_DIR", &redirected)
                .env("GIT_WORK_TREE", &temp)
                .env("GIT_INDEX_FILE", temp.join("poison-index"))
                .env("GIT_OBJECT_DIRECTORY", temp.join("objects"));
        } else {
            child
                .env("GIT_CONFIG_COUNT", "1")
                .env("GIT_CONFIG_KEY_0", "core.bare")
                .env("GIT_CONFIG_VALUE_0", "true");
        }
        let output = child.output().unwrap();
        assert!(
            output.status.success(),
            "Git environment escaped fixture isolation ({variant})"
        );
        assert_eq!(std::fs::read_dir(&redirected).unwrap().count(), 0);
        std::fs::remove_dir_all(temp).unwrap();
    }
}
