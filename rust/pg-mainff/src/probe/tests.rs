use super::*;
use crate::process::{Error, Output};
type Query = dyn Fn(&[&str]) -> Result<Output, Error>;
struct Fake(Box<Query>);
impl Runner for Fake {
    fn run(&self, args: &[&str]) -> Result<Output, Error> {
        (self.0)(args)
    }
}
fn output(s: &str) -> Result<Output, Error> {
    Ok(Output {
        code: 0,
        out: s.into(),
        err: String::new(),
    })
}
#[test]
fn launch_inventory() {
    for (exe, args, windows, expected) in [
        (
            "python3",
            "python3 -m plangraph.keeper.daemon --config fixture.json",
            false,
            true,
        ),
        (
            "python3",
            "python3 -u /repo/src/plangraph/keeper/daemon.py --config fixture.json",
            false,
            true,
        ),
        (
            "pypy3",
            "pypy3 -X dev src/plangraph/keeper/daemon.py",
            false,
            true,
        ),
        ("python3", "python3 -m plangraph.keeper", false, true),
        (
            "vim",
            "vim /repo/src/plangraph/keeper/daemon.py",
            false,
            false,
        ),
        ("ps", "ps -A -o pid= -o comm= -o args=", false, false),
        (
            "python3",
            "python3 editor.py /repo/src/plangraph/keeper/daemon.py",
            false,
            false,
        ),
        (
            "python3",
            "python3 -c 'print(123)' /repo/src/plangraph/keeper/daemon.py",
            false,
            false,
        ),
        (
            "python3",
            "python3 -m plangraph.keeper.cli --help",
            false,
            false,
        ),
        ("python3", "python3 -c \"unterminated", false, false),
        (
            "pypy3.exe",
            r"pypy3.exe C:\repo\plangraph\keeper\daemon.py",
            true,
            true,
        ),
        (
            "python.exe",
            "python.exe -m plangraph.keeper.daemon",
            true,
            true,
        ),
        (
            "python.exe",
            r#""C:\Program Files\python.exe" -u "C:\my repo\plangraph\keeper\daemon.py""#,
            true,
            true,
        ),
        (
            "notepad.exe",
            r"notepad.exe C:\repo\plangraph\keeper\daemon.py",
            true,
            false,
        ),
        (
            "powershell.exe",
            "powershell.exe probe plangraph.keeper.daemon",
            true,
            false,
        ),
        ("System Idle Process", "", true, false),
        (
            "python3",
            "python3 -W ignore -- src/plangraph/keeper/daemon.py",
            false,
            true,
        ),
    ] {
        assert_eq!(keeper_launch(exe, args, windows), expected, "{args}");
    }
}
#[test]
fn empty_ps_args_does_not_hide_later_keeper() {
    let run = Fake(Box::new(|args| {
        assert_eq!(
            args,
            ["ps", "-A", "-o", "pid=", "-o", "comm=", "-o", "args="]
        );
        output("4242 python3\n123 python3 python3 -m plangraph.keeper.daemon\n")
    }));
    assert_eq!(process_pid(&run, false, None), Ok(123));
}
#[test]
fn unrelated_process_and_self_are_excluded() {
    let run = Fake(Box::new(|_| {
        output(&format!(
            "123 vim vim daemon.py\n{} python3 python3 -m plangraph.keeper.daemon\n",
            std::process::id()
        ))
    }));
    assert_eq!(process_pid(&run, false, None), Ok(0));
}
#[test]
fn unit_pid_uses_existing_service() {
    let run = Fake(Box::new(|args| {
        assert_eq!(
            args,
            [
                "systemctl",
                "--user",
                "show",
                "plangraph-keeper.service",
                "--property=LoadState",
                "--property=MainPID"
            ]
        );
        output("LoadState=loaded\nMainPID=123\n")
    }));
    assert_eq!(main_pid(&run, "plangraph-keeper.service"), Ok(123));
}
#[test]
fn process_failures_have_fixed_diagnostics() {
    for (error, cause) in [
        (Error::Missing, "unavailable"),
        (Error::Unavailable, "unavailable"),
        (Error::Timeout, "timed out"),
    ] {
        let run = Fake(Box::new(move |_| Err(error.clone())));
        assert_eq!(
            process_pid(&run, false, None),
            Err(format!("keeper process query {cause}"))
        );
    }
    for (code, data, cause) in [
        (2, "private fixture output", "failed"),
        (0, "bad", "unavailable"),
    ] {
        let run = Fake(Box::new(move |_| {
            Ok(Output {
                code,
                out: data.into(),
                err: "private fixture stderr".into(),
            })
        }));
        assert_eq!(
            process_pid(&run, false, None),
            Err(format!("keeper process query {cause}"))
        );
    }
}
#[test]
fn windows_liveness_is_non_signaling() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("loaded.json");
    // PID 1 exists on POSIX. Only the injected Windows query determines liveness.
    fs::write(
        &path,
        serde_json::json!({"pid": 1, "loaded_sha": "a".repeat(40)}).to_string(),
    )
    .unwrap();
    for live in [true, false] {
        let run = Fake(Box::new(move |args| {
            assert_eq!(
                &args[..4],
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command"
                ]
            );
            assert!(args[4].contains("Get-CimInstance Win32_Process"));
            output(if live {
                r#"[{"ProcessId":0,"Name":"System Idle Process","CommandLine":null},{"ProcessId":1,"Name":"python.exe","CommandLine":"python.exe -m plangraph.keeper.daemon"}]"#
            } else {
                "[]"
            })
        }));
        assert_eq!(
            running(&path, &run, true).unwrap(),
            if live { Some("a".repeat(40)) } else { None }
        );
    }
}
#[test]
fn console_entry_points_match_verified_inventory() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let metadata = fs::read_to_string(root.join("pyproject.toml")).unwrap();
    assert!(
        !metadata.contains("[project.scripts]"),
        "Extend keeper launch detection for new console entry points"
    );
    assert!(!root.join("src/plangraph/keeper/__main__.py").exists());
}
