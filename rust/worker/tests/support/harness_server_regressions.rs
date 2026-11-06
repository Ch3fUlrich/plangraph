// Included in harness_server::tests to exercise the actual supervised path.
fn retry_fake_server_spawn<T>(mut run: impl FnMut() -> std::io::Result<T>) -> std::io::Result<T> {
    // Another test's fork can temporarily inherit the script's write descriptor,
    // even after write_script closes it here. Retry only that transient error;
    // never hide other failures or wait indefinitely for the inherited fd.
    for attempt in 0..20 {
        match run() {
            Err(error) if error.raw_os_error() == Some(libc::ETXTBSY) && attempt < 19 => {
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            result => return result,
        }
    }
    unreachable!()
}

fn serve_probe(script: &str, opts: &Opts, cancelled: &dyn Fn() -> bool) -> Supervised {
    serve_probe_with_limit(script, opts, cancelled, std::time::Duration::from_millis(800))
}

#[test]
fn fake_server_spawn_retry_is_bounded_and_error_specific() {
    let mut attempts = 0;
    let result = retry_fake_server_spawn(|| {
        attempts += 1;
        if attempts < 3 { Err(std::io::Error::from_raw_os_error(libc::ETXTBSY)) } else { Ok(42) }
    });
    assert_eq!(result.unwrap(), 42);
    assert_eq!(attempts, 3);
    attempts = 0;
    let result: std::io::Result<()> = retry_fake_server_spawn(|| {
        attempts += 1;
        Err(std::io::Error::from_raw_os_error(libc::ETXTBSY))
    });
    assert_eq!(result.unwrap_err().raw_os_error(), Some(libc::ETXTBSY));
    assert_eq!(attempts, 20);
    attempts = 0;
    let result: std::io::Result<()> = retry_fake_server_spawn(|| {
        attempts += 1;
        Err(std::io::Error::from_raw_os_error(libc::EACCES))
    });
    assert_eq!(result.unwrap_err().raw_os_error(), Some(libc::EACCES));
    assert_eq!(attempts, 1);
}

fn serve_probe_with_limit(script: &str, opts: &Opts, cancelled: &dyn Fn() -> bool, limit: std::time::Duration) -> Supervised {
    let dir = tempfile::tempdir().unwrap();
    let home = tempfile::tempdir().unwrap();
    let bin = crate::testutil::write_script(dir.path(), "oc-regression", script);
    retry_fake_server_spawn(|| run_opencode_server(
        &Opencode {
            bin: bin.to_string_lossy().into_owned(),
        },
        &HarnessRun {
            workdir: dir.path(),
            model: "some/model",
            prompt: "p",
        },
        home.path(),
        opts,
        cancelled,
        None,
        limit,
    ))
    .unwrap()
}

#[test]
fn captured_streams_drive_stderr_readiness_and_json_client_argv() {
    for (stdout, stderr) in [
        (
            include_str!("../fixtures/opencode/serve-2.2.5-stdout.txt"),
            include_str!("../fixtures/opencode/serve-2.2.5-stderr.txt"),
        ),
        (
            include_str!("../fixtures/opencode/serve-2.0.32-stdout.txt"),
            include_str!("../fixtures/opencode/serve-2.0.32-stderr.txt"),
        ),
    ] {
        let url = stdout
            .lines()
            .chain(stderr.lines())
            .find_map(server_url)
            .unwrap();
        // Replay the real banner on stderr, with terminal color and a secret
        // announcement. Neither stream's other output belongs to client output.
        let script = format!(
            "#!/bin/sh\nif [ \"$1\" = serve ]; then\nprintf '\\033[32m{}\\033[0m\\n' >&2\necho API_KEY=sentinel >&2\nsleep 5\nelse\nprintf '%s\\n' \"$@\"\nfi\n",
            stdout.trim()
        );
        let r = serve_probe(&script, &Opts::default(), &|| false);
        assert_eq!(
            String::from_utf8(r.output).unwrap(),
            format!("run\n--server\n{url}\n--format\njson\n-m\nsome/model\n--\np\n")
        );
    }
}

#[test]
fn cold_start_has_independent_run_budget() {
    let opts = Opts {
        timeout: Some(std::time::Duration::from_millis(100)),
        ..Opts::default()
    };
    let r = serve_probe(
        "#!/bin/sh\nif [ \"$1\" = serve ]; then\nsleep 0.2\necho 'server listening on http://127.0.0.1:4096'\nsleep 5\nelse\necho ready\nfi\n",
        &opts,
        &|| false,
    );
    assert_eq!(r.code, Some(0));
    assert!(!r.timed_out);
    assert_eq!(r.output, b"ready\n");
}

#[test]
fn startup_failure_is_not_a_run_timeout() {
    let opts = Opts {
        timeout: Some(std::time::Duration::ZERO),
        ..Opts::default()
    };
    let r = serve_probe("#!/bin/sh\nsleep 1\nexit 1\n", &opts, &|| false);
    assert_eq!(r.code, Some(1));
    assert!(!r.timed_out);
}

#[test]
fn injected_startup_limit_is_enforced() {
    let start = std::time::Instant::now();
    let r = serve_probe_with_limit("#!/bin/sh\nsleep 2\n", &Opts::default(), &|| false, std::time::Duration::from_millis(50));
    assert_eq!(r.code, Some(1));
    assert!(!r.timed_out);
    assert!(start.elapsed() < std::time::Duration::from_secs(1));
}
