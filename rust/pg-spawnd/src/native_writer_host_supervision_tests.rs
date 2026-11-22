use super::*;

#[cfg(unix)]
#[test]
fn host_reaps_a_writer_past_the_turn_deadline() {
    host_stop_case("timeout", "command timed out");
}

#[cfg(unix)]
#[test]
fn host_reaps_a_writer_when_disk_crosses_the_stop_floor() {
    host_stop_case("disk", "disk_low: under 2 GB stop floor");
}

#[cfg(unix)]
#[test]
fn host_reaps_a_writer_after_a_cancel_marker() {
    host_stop_case("cancel", "command cancelled");
}

#[cfg(unix)]
fn host_stop_case(mode: &str, expected: &str) {
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let status = pg_worker_core::git_test_support::command(root)
        .args(["init", "-q"])
        .status()
        .unwrap();
    assert!(status.success());
    for (program, script) in [
        ("codex", "#!/bin/sh\necho $$ > writer.pid\nexec sleep 4\n"),
        (
            "df",
            "#!/bin/sh\ncd \"$3\" || exit 1\nfor i in $(seq 100); do [ -s writer.pid ] && break; sleep .01; done\nif [ -f disk-low ]; then printf 'x x x 0\\n'; else printf 'x x x 10485760\\n'; fi\n",
        ),
    ] {
        let executable = root.join(program);
        fs::write(&executable, script).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    }
    if mode == "disk" {
        fs::write(root.join("disk-low"), "").unwrap();
    } else if mode == "cancel" {
        fs::write(root.join("cancelled"), "").unwrap();
    }
    let path = std::env::var_os("PATH").unwrap();
    let tool_path = std::env::join_paths(
        std::iter::once(root.to_path_buf()).chain(std::env::split_paths(&path)),
    )
    .unwrap();
    let mut host = Host {
        repo: root.into(),
        cwd: root.into(),
        run_id: "run-fixture".into(),
        reviews: root.join("reviews"),
        record_path: root.join("record.json"),
        base_ref: String::new(),
        tool_path: tool_path.into_string().unwrap(),
        effort: WRITER_EFFORT.into(),
        state: root.into(),
        brief: String::new(),
        push_ref: None,
        reviews_started: 0,
        turn_timeout: if mode == "timeout" {
            Duration::from_millis(400)
        } else {
            Duration::from_secs(5)
        },
        review_override: None,
        review_cli: vec![],
        python_src: None,
    };
    assert_eq!(host.execute(None, "brief").unwrap_err(), expected, "{mode}");
    let pid: i32 = fs::read_to_string(root.join("writer.pid"))
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_ne!(
        unsafe { libc::kill(pid, 0) },
        0,
        "{mode} left the writer live"
    );
}
