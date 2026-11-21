#![cfg(target_os = "linux")]

use std::{fs, path::Path, process::Command};

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
        r#"import pathlib, signal, subprocess, time

def test_hang():
    child = subprocess.Popen(["sh", "-c", "trap '' TERM INT; exec sleep 60"], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pathlib.Path("pid").write_text(str(child.pid))
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
    let pid = fs::read_to_string(tmp.path().join("pid")).unwrap();
    let stat = fs::read_to_string(Path::new("/proc").join(&pid).join("stat")).ok();
    // Clean up the fixture even if the guard regresses.
    Command::new("kill").args(["-KILL", &pid]).output().unwrap();
    assert!(output.contains("HUNG test_hang.py::test_hang"), "{output}");
    assert!(output.contains("in test_hang"), "{output}");
    assert!(stat.is_none_or(|s| s.rsplit_once(") ").unwrap().1.starts_with('Z')));
}

#[test]
fn rust_hang_names_case_and_kills_child() {
    let tmp = tempfile::tempdir().unwrap();
    let pidfile = tmp.path().join("pid");
    let binary = env!("CARGO_BIN_EXE_pg-test-deadline");
    let output = Command::new(binary)
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", "hang_fixture"])
        .env("PLANGRAPH_TEST_DEADLINE", "1")
        .env("PG_HANG_FIXTURE", &pidfile)
        .output()
        .unwrap();
    let log = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let pid = fs::read_to_string(pidfile).unwrap();
    let stat = fs::read_to_string(Path::new("/proc").join(&pid).join("stat")).ok();
    Command::new("kill").args(["-KILL", &pid]).output().unwrap();
    assert!(!output.status.success(), "{log}");
    assert!(
        log.lines()
            .any(|line| line.starts_with("HUNG ") && line.ends_with("::hang_fixture")),
        "{log}"
    );
    assert!(log.contains("stack for test process"), "{log}");
    assert!(
        log.lines()
            .any(|line| line.starts_with("thread ") || line.starts_with("#0 ")),
        "{log}"
    );
    assert!(stat.is_none_or(|s| s.rsplit_once(") ").unwrap().1.starts_with('Z')));
}

#[test]
fn hang_fixture() {
    let Some(path) = std::env::var_os("PG_HANG_FIXTURE") else {
        return;
    };
    let mut child = Command::new("sh")
        .args(["-c", "trap '' TERM INT; exec sleep 60"])
        .spawn()
        .unwrap();
    fs::write(path, child.id().to_string()).unwrap();
    let _ = child.wait();
}
