use super::*;

/// Both historical and current human-readable serve banners. Never accept a
/// remote address, credentials, path, or arbitrary suffix from server output.
pub(super) fn server_url(line: &str) -> Option<String> {
    let mut plain = String::new();
    let mut chars = line.chars();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            if chars.next() != Some('[') {
                return None;
            }
            for c in chars.by_ref() {
                if ('@'..='~').contains(&c) {
                    break;
                }
            }
        } else {
            plain.push(c);
        }
    }
    let line = plain.as_str();
    let url = line
        .trim()
        .strip_prefix("opencode server listening on ")
        .or_else(|| line.trim().strip_prefix("server listening on "))?;
    let port = url.strip_prefix("http://127.0.0.1:")?.parse::<u16>().ok()?;
    (port != 0).then(|| format!("http://127.0.0.1:{port}"))
}

#[cfg(unix)]
pub(super) fn run_opencode_server(
    h: &dyn Harness,
    run: &HarnessRun,
    home: &Path,
    opts: &Opts,
    cancelled: &dyn Fn() -> bool,
    jail: Option<&JailConfig>,
    startup_limit: std::time::Duration,
) -> std::io::Result<Supervised> {
    use std::io::{BufRead, Read};
    use std::os::unix::process::CommandExt;
    use std::sync::{Mutex, mpsc};
    use std::time::{Duration, Instant};
    let mut random = [0u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
    let password: String = random.iter().map(|b| format!("{b:02x}")).collect();
    // The CLI's standalone child silently drops stderr and can re-exec through
    // procfs paths unavailable to descendants in the Landlock jail. Launch the
    // actual binary explicitly, inside the same confinement as its tools.
    let mut serve = Command::new(h.binary().unwrap_or("opencode"));
    serve
        .args(["serve", "--hostname", "127.0.0.1", "--port", "0"])
        .current_dir(run.workdir)
        .env("OPENCODE_DISABLE_AUTOUPDATE", "1")
        .env(OPENCODE_CONFIG_ENV, opencode_permission_config())
        .env("OPENCODE_PASSWORD", &password)
        .env("OPENCODE_SERVER_PASSWORD", &password);
    let mut serve = confined_command(serve, h, run, home, jail)?;
    serve
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = serve.spawn()?;
    struct Server(std::process::Child);
    impl Drop for Server {
        fn drop(&mut self) {
            unsafe {
                libc::kill(-(self.0.id() as i32), libc::SIGKILL);
            }
            let _ = self.0.wait();
        }
    }
    let stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let mut server = Server(child);
    let (tx, rx) = mpsc::channel();
    let stdout_tx = tx.clone();
    let reader = std::thread::spawn(move || {
        for line in std::io::BufReader::new(stdout)
            .lines()
            .map_while(Result::ok)
        {
            if let Some(url) = server_url(&line) {
                let _ = stdout_tx.send(url);
            }
            // Password announcements and all other stdout are discarded.
        }
    });
    let tail = Arc::new(Mutex::new(Vec::new()));
    let dest = tail.clone();
    let errors = std::thread::spawn(move || {
        let mut buf = [0; 1024];
        while let Ok(n) = stderr.read(&mut buf) {
            if n == 0 {
                break;
            }
            let mut tail = dest.lock().unwrap();
            tail.extend_from_slice(&buf[..n]);
            for line in tail.split_inclusive(|b| *b == b'\n') {
                if line.last() == Some(&b'\n')
                    && let Some(url) = server_url(&String::from_utf8_lossy(line))
                {
                    let _ = tx.send(url);
                }
            }
            if tail.len() > 65536 {
                let excess = tail.len() - 65536;
                tail.drain(..excess);
            }
        }
    });
    let start = Instant::now();
    // Startup is not model execution: allow cold initialization independently
    // of the client's run budget and never classify it as a run timeout.
    let url = loop {
        if let Ok(url) = rx.recv_timeout(Duration::from_millis(25)) {
            break Some(url);
        }
        if cancelled() || start.elapsed() >= startup_limit || server.0.try_wait()?.is_some() {
            break None;
        }
    };
    let result = if let Some(url) = url {
        let mut cmd = Command::new(h.binary().unwrap_or("opencode"));
        cmd.args(["run", "--server", &url, "--format", "json"]);
        cmd.env("OPENCODE_DISABLE_AUTOUPDATE", "1")
            .env(OPENCODE_CONFIG_ENV, opencode_permission_config());
        cmd.env("OPENCODE_PASSWORD", &password)
            .env("OPENCODE_SERVER_PASSWORD", &password);
        cmd.args(["-m", run.model, "--", run.prompt])
            .current_dir(run.workdir)
            .stdin(Stdio::null());
        supervise_with_cancel(confined_command(cmd, h, run, home, jail)?, opts, cancelled)
    } else {
        drop(server);
        let _ = reader.join();
        let _ = errors.join();
        let raw = tail.lock().unwrap();
        let safe =
            String::from_utf8_lossy(&raw).replace(&password, pg_worker_core::redact::REDACTED);
        let last = pg_worker_core::redact::redacted_tail(&safe, 1, 1024);
        return Ok(Supervised {
            code: Some(1),
            signal: None,
            stalled: false,
            timed_out: false,
            cancelled: cancelled(),
            output: format!("opencode server startup failed: {last}").into_bytes(),
        });
    };
    drop(server);
    let _ = reader.join();
    let _ = errors.join();
    result.map(|mut result| {
        result.output = String::from_utf8_lossy(&result.output)
            .replace(&password, pg_worker_core::redact::REDACTED)
            .into_bytes();
        result
    })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    include!("../tests/support/harness_server_regressions.rs");
    #[test]
    #[should_panic(expected = "Opencode requires the confined private-server path")]
    fn opencode_rejects_unattached_command() {
        Opencode::new().command(&HarnessRun {
            workdir: Path::new("."),
            model: "some/model",
            prompt: "p",
        });
    }

    #[test]
    fn serve_readiness_225_and_2032_are_loopback_only() {
        for banner in [
            include_str!("../tests/fixtures/opencode/serve-2.2.5-stdout.txt"),
            include_str!("../tests/fixtures/opencode/serve-2.0.32-stdout.txt"),
        ] {
            assert!(server_url(banner).is_some());
        }
        assert_eq!(
            server_url("opencode server listening on http://127.0.0.1:4096"),
            Some("http://127.0.0.1:4096".into())
        );
        for line in [
            "server password secret",
            "server listening on http://0.0.0.0:4096",
            "server listening on http://127.0.0.1:0",
            "server listening on http://127.0.0.1:4096/secret",
        ] {
            assert!(server_url(line).is_none());
        }
    }

    #[test]
    fn explicit_server_failure_is_redacted() {
        let dir = tempfile::tempdir().unwrap();
        let bin = crate::testutil::write_script(
            dir.path(),
            "oc-fail",
            "#!/bin/sh\necho 'API_KEY=sentinel-startup-secret' >&2\nexit 1\n",
        );
        let home = tempfile::tempdir().unwrap();
        let r = retry_fake_server_spawn(|| {
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
                &Opts::default(),
                &|| false,
                None,
            )
        })
        .unwrap();
        let output = String::from_utf8_lossy(&r.output);
        assert!(output.starts_with("opencode server startup failed:"));
        assert!(!output.contains("sentinel-startup-secret"));
        assert!(output.contains(pg_worker_core::redact::REDACTED));
    }

    #[test]
    #[ignore = "set OPENCODE_PROBE_BINARY; private loopback server, real run --help, no model call"]
    fn real_opencode_serve_readiness() {
        let dir = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        // Execute the real CLI parser with the complete production client argv.
        // --help prevents a provider request while proving the flag contract.
        let real = std::env::var("OPENCODE_PROBE_BINARY").expect("explicit probe binary");
        let script = format!(
            "#!/bin/sh\nif [ \"$1\" = serve ]; then exec '{}' \"$@\"; fi\nshift\nexec '{}' run --help \"$@\"\n",
            real, real
        );
        let bin = crate::testutil::write_script(dir.path(), "oc-probe", &script);
        let r = retry_fake_server_spawn(|| {
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
                &Opts::default(),
                &|| false,
                None,
            )
        })
        .unwrap();
        assert_eq!(r.code, Some(0), "{}", String::from_utf8_lossy(&r.output));
        assert_run_help(&String::from_utf8_lossy(&r.output));
    }

    fn assert_run_help(help: &str) {
        for required in [
            "opencode run [flags]",
            "--server string",
            "--model, -m string",
            "--format choice",
            "json",
        ] {
            assert!(help.contains(required), "missing CLI contract: {required}");
        }
    }

    #[test]
    fn captured_real_run_help_contract() {
        // Complete `run --help` captures from both versions.
        // 2.2.5 lists --server under GLOBAL FLAGS; 2.0.32 under FLAGS.
        for help in [
            include_str!("../tests/fixtures/opencode/run-help-2.2.5.txt"),
            include_str!("../tests/fixtures/opencode/run-help-2.0.32.txt"),
        ] {
            assert_run_help(help);
            for header in [
                "DESCRIPTION\n",
                "USAGE\n",
                "ARGUMENTS\n",
                "FLAGS\n",
                "GLOBAL FLAGS\n",
            ] {
                assert!(help.contains(header), "missing help section: {header}");
            }
            assert!(help.contains("--auto"));
            assert!(help.contains("--print-logs"));
        }
    }
}
