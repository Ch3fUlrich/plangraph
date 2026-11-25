use super::*;
use std::process::Command;

fn host(dir: &Path) -> Host {
    let cwd = dir.join("wt");
    fs::create_dir(&cwd).unwrap();
    Host {
        repo: cwd.clone(),
        run_id: "run-clean".into(),
        reviews: cwd.join("reviews"),
        cwd,
        record_path: dir.join("record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: dir.to_path_buf(),
        brief: "brief".into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec!["false".into()],
        python_src: None,
    }
}

fn committed(dir: &Path) -> Host {
    let host = host(dir);
    host.git(&["init", "-q"]).unwrap();
    fs::create_dir_all(host.cwd.join("tools")).unwrap();
    fs::create_dir_all(host.cwd.join("reviews")).unwrap();
    fs::write(host.cwd.join("tools/gate.py"), "ok\n").unwrap();
    fs::write(host.cwd.join("reviews/kept.txt"), "tracked evidence\n").unwrap();
    host.git(&["add", "."]).unwrap();
    output(
        Command::new("git")
            .arg("-C")
            .arg(&host.cwd)
            .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
            .args(["commit", "-q", "-m", "base"]),
    )
    .unwrap();
    host
}

#[test]
fn a_committed_tree_with_untracked_review_artifacts_is_clean() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = committed(dir.path());
    fs::write(host.cwd.join("reviews/run-1.json"), "{}").unwrap();
    host.clean().unwrap();
}

#[test]
fn a_changed_tracked_file_is_never_clean_even_under_the_review_directory() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = committed(dir.path());
    fs::write(host.cwd.join("tools/gate.py"), "replacement gate\n").unwrap();
    let error = host.clean().unwrap_err();
    assert!(error.contains("tools/gate.py"), "{error}");
    host.git(&["checkout", "--", "tools/gate.py"]).unwrap();
    host.clean().unwrap();
    fs::write(host.cwd.join("reviews/kept.txt"), "edited evidence\n").unwrap();
    let error = host.clean().unwrap_err();
    assert!(error.contains("reviews/kept.txt"), "{error}");
}

#[test]
fn an_untracked_file_outside_the_review_directory_is_not_clean() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = committed(dir.path());
    fs::write(host.cwd.join("tools/extra.py"), "x\n").unwrap();
    assert!(host.clean().unwrap_err().contains("tools/extra.py"));
}
