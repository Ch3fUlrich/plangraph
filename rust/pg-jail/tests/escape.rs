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
    for path in ["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/dev"] {
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
    Command::new("systemd-run")
        .args(["--user", "--scope", "--quiet", "/bin/true"])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
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
    let out = jail(work.path(), "cat \"$SECRET\"")
        .env("SECRET", file.as_os_str())
        .output()
        .unwrap();
    assert!(!out.status.success());
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
        eprintln!("SKIP: systemd user scope unavailable");
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
    let status = cmd.status().unwrap();
    assert!(!status.success(), "memory hog unexpectedly succeeded");
    assert_ne!(
        status.code(),
        Some(124),
        "memory hog hit the external timeout"
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
    let status = cmd.status().unwrap();
    assert!(!status.success(), "fork bomb unexpectedly succeeded");
}
