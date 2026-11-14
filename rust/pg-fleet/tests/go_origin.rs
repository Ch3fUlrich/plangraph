mod support;
use support::{git, isolated};
#[test]
fn gate_fetches_origin_main_and_ignores_working_tree_row() {
    let d = tempfile::tempdir().unwrap();
    let home = d.path().join("home");
    let origin = d.path().join("origin.git");
    let repo = d.path().join("clone");
    git(
        d.path(),
        &home,
        &[
            "init",
            "-q",
            "--bare",
            "-b",
            "main",
            origin.to_str().unwrap(),
        ],
    );
    git(
        d.path(),
        &home,
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            repo.to_str().unwrap(),
        ],
    );
    git(
        &repo,
        &home,
        &["commit", "-q", "--allow-empty", "-m", "base"],
    );
    git(&repo, &home, &["push", "-q", "origin", "HEAD:main"]);
    let log = repo.join("router/docs/decisions-log.md");
    std::fs::create_dir_all(log.parent().unwrap()).unwrap();
    let head = "a".repeat(40);
    std::fs::write(
        &log,
        format!("| D-1 | p | GO-HEAD {head}; PLAN p:ITEM conforms=yes deviations=none |\n"),
    )
    .unwrap();
    let run = || {
        isolated(env!("CARGO_BIN_EXE_go-row-gate"), &home)
            .args([
                "--go",
                "D-1",
                "--head",
                &head,
                "--key",
                "p:ITEM",
                "--repo",
                repo.to_str().unwrap(),
            ])
            .env_remove("PG_PGK")
            .output()
            .unwrap()
    };
    let out = run();
    assert_eq!(out.status.code(), Some(5));
    assert_eq!(
        out.stdout,
        b"STOP: no log on origin/main\n",
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    git(&repo, &home, &["add", "-A"]);
    git(&repo, &home, &["commit", "-q", "-m", "log"]);
    git(&repo, &home, &["push", "-q", "origin", "HEAD:main"]);
    std::fs::write(log, "local log has no approval\n").unwrap();
    let out = run();
    assert_eq!(out.status.code(), Some(5));
    assert_eq!(out.stdout, b"STOP: keeper wrapper unavailable\n");
    let cmd = isolated("git", &home);
    assert!(cmd
        .get_envs()
        .filter(|(k, _)| k.to_string_lossy().starts_with("GIT_"))
        .all(|(k, _)| [
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL"
        ]
        .contains(&k.to_string_lossy().as_ref())));
}
