use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_pg-jail");

fn landlock_ok() -> bool {
    let Ok(tmp) = tempfile::tempdir() else {
        return false;
    };
    let status = Command::new(BIN)
        .arg("--rw")
        .arg(tmp.path())
        .arg("--ro")
        .arg("/usr")
        .arg("--ro")
        .arg("/bin")
        .arg("--ro")
        .arg("/lib")
        .arg("--ro")
        .arg("/lib64")
        .arg("--")
        .arg("/bin/true")
        .status();
    if let Ok(status) = status {
        if status.code() == Some(78) {
            eprintln!("SKIP: landlock unsupported");
            return false;
        }
    }
    true
}

fn add_ro(cmd: &mut Command) {
    cmd.arg("--proc-self");
    for path in ["/usr", "/bin", "/lib", "/lib64", "/etc", "/dev"] {
        if Path::new(path).exists() {
            cmd.arg("--ro").arg(path);
        }
    }
}

fn jail(work: &Path, script: &str) -> Command {
    let mut cmd = Command::new(BIN);
    cmd.arg("--rw").arg(work);
    add_ro(&mut cmd);
    cmd.arg("--").arg("sh").arg("-c").arg(script);
    cmd
}

fn systemd_scope_ok() -> bool {
    if std::env::var_os("PG_JAIL_TEST_SKIP_RESOURCE_LIMITS").is_some() {
        eprintln!("SKIP: resource limits explicitly disabled by PG_JAIL_TEST_SKIP_RESOURCE_LIMITS");
        return false;
    }

    for command in [
        vec!["/bin/true"],
        // Read the controller while the scope is alive: systemd removes its
        // cgroup after exit, and accepting properties alone does not prove
        // that the host actually enabled the memory controller.
        vec![
            "--",
            "sh",
            "-c",
            r#"path=$(awk -F: '$1 == 0 { print $3 }' /proc/self/cgroup)
test -n "$path" || { echo 'no unified cgroup path' >&2; exit 1; }
limit="/sys/fs/cgroup$path/memory.max"
value=$(cat "$limit") || exit 1
test "$value" = 67108864 || {
    echo "memory limit not enforced: $limit = $value (expected 67108864)" >&2
    exit 1
}"#,
        ],
    ] {
        let output = Command::new("systemd-run")
            .args([
                "--user",
                "--scope",
                "--quiet",
                "-p",
                "MemoryMax=64M",
                "-p",
                "TasksMax=32",
            ])
            .args(command)
            .output();
        match output {
            Ok(out) if out.status.success() => {}
            Ok(out) => {
                eprintln!(
                    "SKIP: resource-limit capability probe failed ({}): {}",
                    out.status,
                    String::from_utf8_lossy(&out.stderr).trim()
                );
                return false;
            }
            Err(err) => {
                eprintln!("SKIP: cannot run systemd resource-limit capability probe: {err}");
                return false;
            }
        }
    }
    true
}

#[test]
fn write_inside_ok() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let out = jail(work.path(), "echo x > \"$W/a\"")
        .env("W", work.path())
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(work.path().join("a").exists());
}

#[test]
fn absolute_outside_denied() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let out = jail(work.path(), "echo x > \"$OUT/escape\"")
        .env("OUT", outside.path())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!outside.path().join("escape").exists());
}

#[test]
fn relative_dotdot_denied() {
    if !landlock_ok() {
        return;
    }
    let base = tempfile::tempdir().unwrap();
    let work = base.path().join("work");
    fs::create_dir(&work).unwrap();
    let out = jail(&work, "echo x > ../escape")
        .current_dir(&work)
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!base.path().join("escape").exists());
}

#[test]
fn mnt_denied() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let target = if Path::new("/mnt/c").is_dir() {
        PathBuf::from("/mnt/c/pg-jail-probe")
    } else {
        PathBuf::from("/mnt/pg-jail-probe")
    };
    let out = jail(work.path(), "echo x > \"$TARGET\"")
        .env("TARGET", target.as_os_str())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!target.exists());
}

#[test]
fn home_denied() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let home = tempfile::tempdir().unwrap();
    let out = jail(work.path(), "echo x > \"$HOME/pg-jail-probe\"")
        .env("HOME", home.path())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(!home.path().join("pg-jail-probe").exists());
}

#[test]
fn deny_read() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let secret = tempfile::tempdir().unwrap();
    let file = secret.path().join("secret");
    fs::write(&file, "secret").unwrap();
    let mut cmd = Command::new(BIN);
    cmd.arg("--rw").arg(work.path());
    cmd.arg("--deny-read").arg(&file);
    add_ro(&mut cmd);
    let out = cmd
        .args(["--", "sh", "-c", "cat \"$SECRET\""])
        .env("SECRET", file.as_os_str())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("Permission denied"));
}

#[test]
fn proc_self_and_sibling_isolation() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    // exec keeps the jail PID: a forked cat would have a different /proc/self.
    let out = jail(work.path(), "exec cat /proc/self/status")
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    assert!(String::from_utf8_lossy(&out.stdout).contains("Pid:"));

    let mut sibling = Command::new("sleep").arg("30").spawn().unwrap();
    let sibling_path = format!("/proc/{}/environ", sibling.id());
    assert!(fs::read(&sibling_path).is_ok());
    let outputs: Vec<_> = ["/proc/1/environ", sibling_path.as_str()]
        .into_iter()
        .map(|path| {
            let mut cmd = Command::new(BIN);
            cmd.arg("--rw").arg(work.path()).args(["--ro", "/proc"]);
            add_ro(&mut cmd);
            cmd.args(["--", "cat", path]).output().unwrap()
        })
        .collect();
    sibling.kill().unwrap();
    sibling.wait().unwrap();
    for out in outputs {
        assert!(!out.status.success(), "{out:?}");
        assert!(
            String::from_utf8_lossy(&out.stderr).contains("Permission denied"),
            "{out:?}"
        );
    }
}

#[test]
fn rename_and_link_across_rw_roots() {
    if !landlock_ok() {
        return;
    }
    let base = tempfile::tempdir().unwrap();
    let a = base.path().join("a");
    let b = base.path().join("b");
    fs::create_dir(&a).unwrap();
    fs::create_dir(&b).unwrap();
    fs::write(a.join("source"), "data").unwrap();
    let mut cmd = Command::new(BIN);
    cmd.arg("--rw").arg(&a).arg("--rw").arg(&b);
    add_ro(&mut cmd);
    let out = cmd
        .args([
            "--",
            "sh",
            "-c",
            "mv \"$A/source\" \"$B/moved\" && ln \"$B/moved\" \"$A/linked\"",
        ])
        .env("A", &a)
        .env("B", &b)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    assert_eq!(fs::read(a.join("linked")).unwrap(), b"data");
}

#[test]
fn rw_on_a_device_file_works() {
    if !landlock_ok() {
        return;
    }

    let work = tempfile::tempdir().unwrap();
    let mut cmd = Command::new(BIN);
    cmd.arg("--rw").arg(work.path());
    cmd.arg("--rw").arg("/dev/null");
    add_ro(&mut cmd);
    cmd.arg("--").arg("sh").arg("-c").arg("echo x > /dev/null");

    let status = cmd.status().unwrap();
    assert!(status.success(), "expected exit 0, got {status:?}");
}

#[test]
fn fail_closed_missing_path() {
    if !landlock_ok() {
        return;
    }
    let status = Command::new(BIN)
        .args(["--rw", "/nonexistent-dir-xyz", "--", "/bin/true"])
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(126));
}

#[test]
fn usage() {
    if !landlock_ok() {
        return;
    }
    let work = tempfile::tempdir().unwrap();
    let status = Command::new(BIN)
        .arg("--rw")
        .arg(work.path())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(64));
}

#[test]
fn memory_hog_and_fork_bomb() {
    if !landlock_ok() {
        return;
    }
    if !systemd_scope_ok() {
        return;
    }

    let work = tempfile::tempdir().unwrap();

    let mut cmd = Command::new("timeout");
    cmd.arg("30").arg("systemd-run");
    cmd.args(["--user", "--scope", "--quiet"]);
    cmd.args([
        "-p",
        "MemoryMax=64M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "TasksMax=32",
    ]);
    cmd.arg("--").arg(BIN).arg("--rw").arg(work.path());
    add_ro(&mut cmd);
    cmd.arg("--")
        .arg("sh")
        .arg("-c")
        .arg("x=a; while :; do x=\"$x$x\"; done");
    let out = cmd.output().unwrap();
    use std::os::unix::process::ExitStatusExt;
    assert!(
        out.status.signal() == Some(9) || out.status.code() == Some(137),
        "expected OOM SIGKILL, got {out:?}"
    );

    let mut cmd = Command::new("timeout");
    cmd.arg("30").arg("systemd-run");
    cmd.args(["--user", "--scope", "--quiet"]);
    cmd.args([
        "-p",
        "MemoryMax=64M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "TasksMax=32",
    ]);
    cmd.arg("--")
        .arg("timeout")
        .arg("20")
        .arg(BIN)
        .arg("--rw")
        .arg(work.path());
    add_ro(&mut cmd);
    cmd.arg("--")
        .arg("sh")
        .arg("-c")
        .arg("f() { f | f & }; f; sleep 30");
    let out = cmd.output().unwrap();
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("Cannot fork") || stderr.contains("Resource temporarily unavailable"),
        "expected task-limit refusal, got {out:?}"
    );
    assert!(!out.status.success(), "fork bomb unexpectedly succeeded");
}
