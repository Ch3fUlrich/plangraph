mod support;
use support::{git, isolated};
#[test]
fn real_merge_history_project_filter_and_default_json_snapshot() {
    let d = tempfile::tempdir().unwrap();
    let home = d.path().join("home");
    let repo = d.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    git(&repo, &home, &["init", "-q", "-b", "main"]);
    git(
        &repo,
        &home,
        &["commit", "-q", "--allow-empty", "-m", "base"],
    );
    let make = |key: &str, branch: &str, pr: &str| {
        git(&repo, &home, &["switch", "-q", "-c", branch]);
        git(
            &repo,
            &home,
            &["commit", "-q", "--allow-empty", "-m", branch],
        );
        let head = git(&repo, &home, &["rev-parse", "HEAD"]);
        git(&repo, &home, &["switch", "-q", "main"]);
        git(
            &repo,
            &home,
            &[
                "merge",
                "-q",
                "--no-ff",
                branch,
                "-m",
                &format!("Merge pull request #{pr}\n\nPlan-Item: {key}"),
            ],
        );
        (git(&repo, &home, &["rev-parse", "HEAD"]), head)
    };
    let old = make("sample:POST", "old", "518");
    let close = make("sample:CLOSE", "close", "519");
    let missing = make("sample:MISSING", "missing", "517");
    let post = make("sample:POST", "post", "518");
    let items = d.path().join("items.json");
    std::fs::write(&items,r#"{"items":[{"key":"sample:POST"},{"key":"sample:CLOSE"},{"key":"sample:MISSING"},{"key":"sample:OPEN"},{"key":"sample:DONE","state":"done"},{"key":"other:ITEM"}]}"#).unwrap();
    let log = d.path().join("log.md");
    std::fs::write(&log,format!("| D-4 | sample | GO-HEAD {}; run-bbbb |\n| D-3 | sample | GO-HEAD {}; run-abcd, run-cdef, run-abcd |\n| D-2 | sample | GO-HEAD {} |\n",old.1,&post.1[..8],close.1)).unwrap();
    let out = d.path().join("report.tsv");
    let absent = d.path().join("missing.tsv");
    let args = [
        "--repo",
        repo.to_str().unwrap(),
        "--log",
        log.to_str().unwrap(),
        "--out",
        out.to_str().unwrap(),
        "--out-missing",
        absent.to_str().unwrap(),
        "--project",
        "sample",
    ];
    let output = isolated(env!("CARGO_BIN_EXE_inreview-backfill"), &home)
        .args(args)
        .args(["--items", items.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        output.stdout,
        b"post-verdicts=1 close-by-decision=1 no-go-row=1 not-merged=1\n"
    );
    let text = std::fs::read_to_string(&out).unwrap();
    assert!(text.contains(&format!(
        "sample:POST\t{}\t{}\tD-3\tgo-head\trun-abcd,run-cdef\tpost-verdicts\n",
        post.0, post.1
    )));
    assert!(!text.contains(&old.0));
    assert!(text.contains(&missing.0));
    assert!(std::fs::read_to_string(&absent)
        .unwrap()
        .contains("sample:OPEN\t\t\t\t\t\tnot-merged\n"));
    let bin = d.path().join("bin");
    std::fs::create_dir(&bin).unwrap();
    #[cfg(windows)] std::fs::write(bin.join("bash.cmd"),"@echo off\r\nif not \"%~2\"==\"snapshot\" exit /b 9\r\nif not \"%~3\"==\"--json\" exit /b 9\r\ntype \"%BACKFILL_ITEMS%\"\r\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let p = bin.join("bash");
        std::fs::write(&p,"#!/bin/sh\n[ \"$2\" = snapshot ] && [ \"$3\" = --json ] || exit 9\n/bin/cat \"$BACKFILL_ITEMS\"\n").unwrap();
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let mut paths = vec![bin];
    paths.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    let output = isolated(env!("CARGO_BIN_EXE_inreview-backfill"), &home)
        .args(args)
        .args(["--pgk", "fixture-pgk.sh"])
        .env("PATH", std::env::join_paths(paths).unwrap())
        .env("BACKFILL_ITEMS", items)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        output.stdout,
        b"post-verdicts=1 close-by-decision=1 no-go-row=1 not-merged=1\n"
    );
}
