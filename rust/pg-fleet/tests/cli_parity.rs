#[test]
fn help_and_usage_match_python_goldens() {
    for (exe, name) in [
        (env!("CARGO_BIN_EXE_go-row-gate"), "go-row-gate"),
        (env!("CARGO_BIN_EXE_inreview-backfill"), "inreview-backfill"),
        (env!("CARGO_BIN_EXE_resume-digest"), "resume-digest"),
        (env!("CARGO_BIN_EXE_work-summary"), "work-summary"),
    ] {
        let out = std::process::Command::new(exe)
            .arg("--help")
            .output()
            .unwrap();
        assert!(out.status.success());
        assert!(out.stderr.is_empty());
        assert_eq!(out.stdout, pg_fleet::cli::help(name).as_bytes());
    }
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_handoff-check"))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert_eq!(out.stderr, include_bytes!("../src/usage/handoff-check.txt"));
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_fleet-name"))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(
        String::from_utf8_lossy(&out.stderr),
        include_str!("../src/usage/fleet-name.txt").to_owned() + "\n"
    );
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_resume-digest-carried"))
        .args(["any", "arguments"])
        .output()
        .unwrap();
    assert!(out.status.success());
    assert!(out.stdout.is_empty() && out.stderr.is_empty());
}
#[test]
fn role_and_head_errors_preserve_usage_and_reason() {
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_resume-digest"))
        .args(["--role=../etc/passwd"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(String::from_utf8_lossy(&out.stderr),"usage: resume-digest [-h] [--role ROLE]\nresume-digest: error: bad --role '../etc/passwd'\n");
    let out = std::process::Command::new(env!("CARGO_BIN_EXE_go-row-gate"))
        .args(["--go=D-1", "--head=aaaaaaaa"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(out.stdout.is_empty());
    assert!(String::from_utf8_lossy(&out.stderr)
        .ends_with("go-row-gate: error: --head must be exactly 40 hex characters\n"));
}
#[cfg(unix)]
#[test]
fn subprocess_deadline_includes_inherited_output_pipes() {
    let mut command = std::process::Command::new("/bin/sh");
    command.args(["-c", "sleep 2 & exit 0"]);
    let start = std::time::Instant::now();
    assert_eq!(
        pg_fleet::process::capture(&mut command, 1).unwrap_err(),
        "TimeoutExpired"
    );
    assert!(start.elapsed() < std::time::Duration::from_millis(1800));
}
