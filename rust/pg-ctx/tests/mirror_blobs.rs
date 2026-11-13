//! Named blobs are resolved at the accepted main, never the caller's worktree.
use pg_ctx::mirror::{Alarm, Mirror, Watermark};
use std::{collections::BTreeMap, fs, path::Path};

fn git(dir: &Path, args: &[&str]) -> String {
    let out = pg_worker_core::workspace_resolver::git_command(dir)
        .args(args)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", dir.join("absent-config"))
        .env("GIT_AUTHOR_NAME", "Mirror Test")
        .env("GIT_COMMITTER_NAME", "Mirror Test")
        .env("GIT_AUTHOR_EMAIL", "mirror@example.invalid")
        .env("GIT_COMMITTER_EMAIL", "mirror@example.invalid")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().into()
}

#[test]
fn named_blob_shas_round_trip_and_advance_with_main() {
    let tmp = tempfile::tempdir().unwrap();
    let work = tmp.path().join("work");
    fs::create_dir(&work).unwrap();
    git(&work, &["init", "--initial-branch=main"]);
    fs::create_dir(work.join("docs")).unwrap();
    fs::write(work.join("README.md"), "first\n").unwrap();
    fs::write(work.join("docs/a file.md"), "named\n").unwrap();
    git(&work, &["add", "."]);
    git(&work, &["commit", "-m", "first"]);
    let origin = tmp.path().join("origin.git");
    git(
        tmp.path(),
        &[
            "clone",
            "--bare",
            work.to_str().unwrap(),
            origin.to_str().unwrap(),
        ],
    );
    let config = tmp.path().join("repos.json");
    fs::write(
        &config,
        serde_json::to_vec(&serde_json::json!({"demo": origin})).unwrap(),
    )
    .unwrap();
    let mirror = Mirror::from_repos(&config, &tmp.path().join("mirrors")).unwrap();
    let first = mirror
        .fetch_named("demo", &["README.md", "docs/a file.md"])
        .unwrap();
    assert_eq!(first.main_head, git(&work, &["rev-parse", "HEAD"]));
    assert_eq!(
        first.blob_shas,
        BTreeMap::from([
            (
                "README.md".into(),
                git(&work, &["rev-parse", "HEAD:README.md"])
            ),
            (
                "docs/a file.md".into(),
                git(&work, &["rev-parse", "HEAD:docs/a file.md"])
            ),
        ])
    );
    let path = mirror
        .mirror_dir("demo")
        .unwrap()
        .with_file_name("watermark.json");
    let bytes = fs::read(&path).unwrap();
    assert_eq!(serde_json::from_slice::<Watermark>(&bytes).unwrap(), first);
    assert_eq!(mirror.watermark("demo").unwrap(), Some(first.clone()));
    fs::write(work.join("README.md"), "second\n").unwrap();
    git(&work, &["commit", "-am", "second"]);
    git(&work, &["push", origin.to_str().unwrap(), "main"]);
    for bad in ["absent", "docs", "../README.md", "/README.md", ""] {
        assert_eq!(mirror.fetch_named("demo", &[bad]), Err(Alarm::FetchFailed));
        assert_eq!(fs::read(&path).unwrap(), bytes);
        assert_eq!(
            git(&mirror.mirror_dir("demo").unwrap(), &["rev-parse", "main"]),
            first.main_head
        );
    }
    // Ordinary refreshes retain the named set and recompute its shas.
    let second = mirror.fetch("demo").unwrap();
    assert_eq!(
        second.blob_shas["README.md"],
        git(&work, &["rev-parse", "HEAD:README.md"])
    );
    assert_ne!(second.blob_shas["README.md"], first.blob_shas["README.md"]);
    assert_eq!(
        second.blob_shas["docs/a file.md"],
        first.blob_shas["docs/a file.md"]
    );
    assert_eq!(mirror.watermark("demo").unwrap(), Some(second.clone()));
    git(
        &work,
        &[
            "push",
            "--force",
            origin.to_str().unwrap(),
            &format!("{}:main", first.main_head),
        ],
    );
    assert_eq!(mirror.fetch("demo"), Err(Alarm::ForcePush));
    assert_eq!(mirror.watermark("demo").unwrap(), Some(second));
}
