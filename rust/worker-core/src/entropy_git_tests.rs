use super::*;
use crate::entropy::check;
use std::fs;
#[test]
fn entropy_git_temp_repository_collection() {
    let dir = tempfile::tempdir().unwrap();
    let git = Git {
        root: dir.path().to_path_buf(),
        binary: "git".into(),
    };
    let run = |args: &[&str], date: &str| {
        let output = git
            .command()
            .args([
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture",
                "-c",
                "core.hooksPath=/nonexistent-fixture-hooks",
                "-c",
                "commit.gpgsign=false",
            ])
            .args(args)
            .env("GIT_AUTHOR_DATE", date)
            .env("GIT_COMMITTER_DATE", date)
            .output()
            .unwrap();
        assert!(output.status.success(), "temporary git command failed");
    };
    run(&["init", "--quiet"], "2026-11-01T00:00:00Z");
    fs::create_dir(dir.path().join("src")).unwrap();
    fs::create_dir(dir.path().join("tests")).unwrap();
    fs::write(dir.path().join("src/tool.rs"), "code\n".repeat(1000)).unwrap();
    fs::write(dir.path().join("tests/test_tool.rs"), "test\n".repeat(100)).unwrap();
    fs::write(
        dir.path().join(".entropy-baseline.json"),
        serde_json::to_vec(&Baseline::default()).unwrap(),
    )
    .unwrap();
    run(&["add", "."], "2026-11-01T00:00:00Z");
    run(
        &["commit", "--quiet", "-m", "fixture base"],
        "2026-11-01T00:00:00Z",
    );
    let base = git.revision("HEAD").unwrap();
    fs::write(
        dir.path().join("src/name with space.rs"),
        "new\n".repeat(10),
    )
    .unwrap();
    run(&["add", "."], "2026-11-13T00:00:00Z");
    run(
        &["commit", "--quiet", "-m", "fixture head"],
        "2026-11-13T00:00:00Z",
    );
    let p = git.input(&base, "HEAD", String::new()).unwrap();
    assert_eq!(p.changes.len(), 1);
    assert_eq!(p.changes[0].path, "src/name with space.rs");
    assert!(p.changes[0].new);
    assert_eq!(p.changes[0].added, 10);
    assert_eq!(p.week.source, 1000);
    assert_eq!(p.current.source, 1010);
    assert!(check(&p).is_empty());
    assert!(git.revision("--help").is_err());
    assert!(git.revision("missing").is_err());
}
