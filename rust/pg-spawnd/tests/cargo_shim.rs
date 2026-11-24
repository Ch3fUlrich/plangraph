//! The `cargo` link of the native writer sandbox holds the shared lease per command.
#![cfg(target_os = "linux")]
use pg_worker_core::cargo_cache::{lease_holders, try_exclusive_lease};
use pg_worker_core::cargo_command_lease::install_shim;
use std::{os::unix::fs::PermissionsExt, path::Path, process::Command, time::Duration};

fn script(dir: &Path, name: &str, body: &str) {
    let path = dir.join(name);
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

fn wait_for(mut done: impl FnMut() -> bool) -> bool {
    for _ in 0..500 {
        if done() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    false
}

#[test]
fn a_cancelled_cargo_command_strands_neither_the_lease_nor_the_cargo_process() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let pidfile = tmp.path().join("cargo.pid");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(
        &real,
        "cargo",
        &format!("echo $$ > {}\nexec sleep 300", pidfile.display()),
    );
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    let mut child = Command::new(&link)
        .arg("build")
        .env("CARGO_TARGET_DIR", &target)
        .env(
            "PATH",
            format!("{}:{}:/usr/bin:/bin", shim.display(), real.display()),
        )
        .env_remove("PLANGRAPH_CARGO_LEASE_HELD")
        .spawn()
        .unwrap();
    assert!(wait_for(
        || pidfile.exists() && !lease_holders(&target).is_empty()
    ));
    assert_eq!(lease_holders(&target), vec![child.id()]);
    assert!(try_exclusive_lease(&target).unwrap().is_none());
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(
        try_exclusive_lease(&target).unwrap().is_some(),
        "the lease survived the cancelled command"
    );
    let cargo: i32 = std::fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    // SAFETY: signal 0 only probes for existence.
    assert!(
        wait_for(|| unsafe { libc::kill(cargo, 0) } != 0),
        "the cargo child outlived its shim"
    );
}

#[test]
fn the_shim_reports_the_blocking_pid_when_the_target_is_being_reclaimed() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(&real, "cargo", "exit 0");
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    let _trim = try_exclusive_lease(&target).unwrap().unwrap();
    // The wait is bounded by the shim; cap it for the test through a command that has no outer lease.
    let output = Command::new(&link)
        .env("CARGO_TARGET_DIR", &target)
        .env("PATH", format!("{}:{}", shim.display(), real.display()))
        .env("PLANGRAPH_CARGO_SHIM_WAIT_MS", "0")
        .env_remove("PLANGRAPH_CARGO_LEASE_HELD")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    let note = format!("held by pid {}", std::process::id());
    assert_eq!(stderr.matches(&note).count(), 1, "{stderr}");
}

#[test]
fn a_nested_command_under_an_outer_lease_does_not_wait_for_itself() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(&real, "cargo", "exit 5");
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    // Preflight stages artifacts under the exclusive lease and sets the marker for its children.
    let _exclusive = try_exclusive_lease(&target).unwrap().unwrap();
    let status = Command::new(&link)
        .env("CARGO_TARGET_DIR", &target)
        .env("PLANGRAPH_CARGO_LEASE_HELD", &target)
        .env("PATH", format!("{}:{}", shim.display(), real.display()))
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(5));
}

/// Real Codex sandbox: the only writable root is the target, so the lock file beside it can be
/// opened but not created or written. The shim must still hold the lease for the command.
#[test]
#[ignore = "live host check requiring a working Codex sandbox"]
fn the_shim_holds_its_lease_inside_the_writer_sandbox() {
    // Outside /tmp and $TMPDIR, which the sandbox mode keeps writable for builds.
    let dir = tempfile::tempdir_in(env!("CARGO_MANIFEST_DIR")).unwrap();
    let root = dir.path().canonicalize().unwrap();
    let (target, real, shim, workspace) = (
        root.join("target"),
        root.join("real"),
        root.join("shim"),
        root.join("workspace"),
    );
    for d in [&target, &real, &workspace] {
        std::fs::create_dir(d).unwrap();
    }
    // The host creates the lock file before the sandboxed child starts.
    let lock = pg_worker_core::cargo_cache::ensure_lease_file(&target).unwrap();
    let probe = real.join("probe");
    // The fake Cargo asks the kernel who holds the lock while it runs.
    script(
        &real,
        "cargo",
        &format!(
            "grep -c FLOCK /proc/locks > {}; echo cargo-ran",
            probe.display()
        ),
    );
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    let roots = vec![target.clone(), probe.parent().unwrap().to_path_buf()];
    let script_text = format!(
        "touch {sibling} && echo sibling-writable; {link} build; echo exit=$?",
        sibling = root.join("sibling").display(),
        link = link.display()
    );
    let output = Command::new("codex")
        .arg("sandbox")
        .args(pg_worker_core::host_readiness::writer_sandbox_args(&roots).unwrap())
        .args(["--", "env"])
        .arg(format!("CARGO_TARGET_DIR={}", target.display()))
        .arg(format!(
            "PATH={}:{}:/usr/bin:/bin",
            shim.display(),
            real.display()
        ))
        .args(["sh", "-c", &script_text])
        .current_dir(&workspace)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout);
    let errors = String::from_utf8_lossy(&output.stderr);
    assert!(
        text.contains("cargo-ran") && text.contains("exit=0"),
        "{text}{errors}"
    );
    assert!(!text.contains("sibling-writable"), "{text}");
    assert!(
        probe.exists() && std::fs::read_to_string(&probe).unwrap().trim() != "0",
        "no flock was held while the command ran: {}",
        lock.display()
    );
}

#[test]
fn an_idle_writer_between_cargo_steps_releases_the_lease_so_trim_succeeds() {
    let tmp = tempfile::tempdir().unwrap();
    let checkout = tmp.path().join("checkout");
    let target = tmp.path().join("target");
    let profile = target.join("debug");
    std::fs::create_dir_all(profile.join("build")).unwrap();
    std::fs::create_dir(&checkout).unwrap();
    std::fs::write(profile.join(".cargo-lock"), []).unwrap();
    std::fs::write(
        target.join("CACHEDIR.TAG"),
        b"Signature: 8a477f597d28d172789f06886806bc55",
    )
    .unwrap();
    let old = std::time::SystemTime::now() - Duration::from_secs(7 * 3600);
    for n in 0..10 {
        let unit = profile.join("build").join(format!("pg-core-{n:016x}"));
        std::fs::create_dir_all(&unit).unwrap();
        std::fs::write(unit.join("build-script-build"), vec![0u8; 100]).unwrap();
        for p in [unit.join("build-script-build"), unit] {
            std::fs::File::open(p).unwrap().set_modified(old).unwrap();
        }
    }
    pg_worker_core::cargo_cache::ensure_lease_file(&target).unwrap();
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(&real, "cargo", "exit 0");
    let shim = tmp.path().join("shim");
    install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();

    let idle_flag = tmp.path().join("idle.flag");
    let trimmed_flag = tmp.path().join("trimmed.flag");
    let writer_script = format!(
        "cargo build && touch {idle} && while [ ! -f {trimmed} ]; do sleep 0.05; done && cargo check",
        idle = idle_flag.display(),
        trimmed = trimmed_flag.display()
    );
    let mut supervisor = Command::new("sh")
        .args(["-c", &writer_script])
        .env("CARGO_TARGET_DIR", &target)
        .env(
            "PATH",
            format!("{}:{}:/usr/bin:/bin", shim.display(), real.display()),
        )
        .env_remove("PLANGRAPH_CARGO_LEASE_HELD")
        .spawn()
        .unwrap();
    assert!(wait_for(|| idle_flag.exists()), "writer never reached idle");
    assert!(lease_holders(&target).is_empty());
    let report =
        pg_worker_core::disk_runtime::reclaim_before_push_using(&checkout, &target, 700, || {
            Ok((vec![], false))
        })
        .expect("trim between cargo steps must succeed");
    assert!(report.freed_bytes > 0);
    assert!(pg_worker_core::disk_hygiene::scan(&target).unwrap().0 <= 700);
    std::fs::write(&trimmed_flag, "done").unwrap();
    assert!(supervisor.wait().unwrap().success());
}
