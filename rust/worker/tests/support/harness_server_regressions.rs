// Included in harness_server::tests to exercise the actual supervised path.
fn serve_probe(script: &str, opts: &Opts, cancelled: &dyn Fn() -> bool) -> Supervised {
    let dir = tempfile::tempdir().unwrap();
    let home = tempfile::tempdir().unwrap();
    let bin = crate::testutil::write_script(dir.path(), "oc-regression", script);
    run_harness(
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
    )
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
fn cold_start_over_thirty_seconds_has_independent_run_budget() {
    let opts = Opts {
        timeout: Some(std::time::Duration::from_secs(2)),
        ..Opts::default()
    };
    let r = serve_probe(
        "#!/bin/sh\nif [ \"$1\" = serve ]; then\nsleep 31\necho 'server listening on http://127.0.0.1:4096'\nsleep 5\nelse\necho ready\nfi\n",
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
