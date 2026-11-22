#![cfg(target_os = "linux")]

use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::Path,
    process::{Command, Stdio},
};

#[test]
fn pytest_hang_names_case_dumps_stack_and_kills_child() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let tmp = tempfile::tempdir().unwrap();
    fs::copy(root.join("pytest.ini"), tmp.path().join("pytest.ini")).unwrap();
    fs::copy(
        root.join("tests/conftest.py"),
        tmp.path().join("conftest.py"),
    )
    .unwrap();
    let paths = std::env::join_paths([root.join("tests"), root.join("src")]).unwrap();
    fs::write(
        tmp.path().join("test_hang.py"),
        r#"import pathlib, signal, subprocess, sys, time

def test_hang():
    child = subprocess.Popen(["sh", "-c", "trap '' TERM INT; exec sleep 60"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path("pid").write_text(str(child.pid))
    # The detached grandchild explicitly creates a new session and loses its parent.
    subprocess.run([sys.executable, "-c", """
import os, pathlib, time
if os.fork():
    os._exit(0)
os.setsid()
parent = os.getpid()
if os.fork():
    os._exit(0)
while os.getppid() == parent:
    time.sleep(.01)
pathlib.Path("detached").write_text(str(os.getpid()))
time.sleep(60)
"""],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not pathlib.Path("detached").exists():
        time.sleep(.01)
    signal.alarm(4)
    time.sleep(60)
"#,
    )
    .unwrap();
    let result = Command::new("python3")
        .args([
            "-m",
            "pytest",
            "-q",
            "-s",
            "-o",
            "test_deadline=1",
            "test_hang.py",
        ])
        .env("PYTHONPATH", paths)
        .current_dir(tmp.path())
        .output()
        .unwrap();
    assert!(!result.status.success());
    let output = format!(
        "{}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
    let direct_alive = clean_up_fixture(&tmp.path().join("pid"));
    let detached_alive = clean_up_fixture(&tmp.path().join("detached"));
    assert!(output.contains("HUNG test_hang.py::test_hang"), "{output}");
    assert!(output.contains("in test_hang"), "{output}");
    assert!(!direct_alive, "direct child survived");
    assert!(!detached_alive, "double-forked child survived");
}

fn clean_up_fixture(pidfile: &Path) -> bool {
    let pid = fs::read_to_string(pidfile).unwrap();
    let alive = fs::read_to_string(Path::new("/proc").join(&pid).join("stat"))
        .is_ok_and(|stat| !stat.rsplit_once(") ").unwrap().1.starts_with('Z'));
    if alive {
        Command::new("kill").args(["-KILL", &pid]).output().unwrap();
    }
    alive
}

#[test]
fn rust_hang_names_case_and_kills_child() {
    let log = run_rust_hang(None);
    let debugger_available = Command::new("gdb")
        .arg("--version")
        .output()
        .is_ok_and(|output| output.status.success());
    if debugger_available {
        assert!(log.lines().any(|line| line.starts_with("#0 ")), "{log}");
    } else {
        assert!(log.contains("user-space stack unavailable"), "{log}");
    }
}

#[test]
fn rust_hang_reports_when_the_debugger_emits_no_frames() {
    let directory = tempfile::tempdir().unwrap();
    let debugger = directory.path().join("gdb");
    fs::write(&debugger, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&debugger, fs::Permissions::from_mode(0o700)).unwrap();
    let log = run_rust_hang(Some(directory.path()));
    assert!(log.contains("user-space stack unavailable"), "{log}");
}

fn run_rust_hang(debugger_dir: Option<&Path>) -> String {
    let tmp = tempfile::tempdir().unwrap();
    let pidfile = tmp.path().join("pid");
    let mut command = Command::new(env!("CARGO_BIN_EXE_pg-test-deadline"));
    command
        .arg(std::env::current_exe().unwrap())
        .args(["--ignored", "--exact", "hang_fixture"])
        .env("PLANGRAPH_TEST_DEADLINE", "1")
        .env("PG_HANG_FIXTURE", &pidfile);
    if let Some(dir) = debugger_dir {
        let path = std::env::var_os("PATH").unwrap_or_default();
        command.env(
            "PATH",
            std::env::join_paths(
                std::iter::once(dir.to_owned()).chain(std::env::split_paths(&path)),
            )
            .unwrap(),
        );
    }
    let output = command.output().unwrap();
    let log = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!clean_up_fixture(&pidfile), "Rust test child survived");
    assert!(!output.status.success(), "{log}");
    assert!(
        log.lines()
            .any(|line| line.starts_with("HUNG ") && line.ends_with("::hang_fixture")),
        "{log}"
    );
    log
}

#[test]
#[ignore = "subprocess fixture driven by the timeout tests"]
fn hang_fixture() {
    let path = std::env::var_os("PG_HANG_FIXTURE").expect("fixture requires a PID file");
    let mut child = Command::new("setsid")
        .args(["sh", "-c", "trap '' TERM INT; exec sleep 60"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    fs::write(path, child.id().to_string()).unwrap();
    let _ = child.wait();
}
