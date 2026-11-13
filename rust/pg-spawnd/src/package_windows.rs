//! Scheduled task arguments shared by production PowerShell and command fakes.
pub(crate) fn commands(task: &str, runner: &str) -> Vec<Vec<String>> {
    let action = format!(
        "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{runner}\""
    );
    vec![
        vec![
            "/Create", "/TN", task, "/TR", &action, "/SC", "ONCE", "/ST", "23:59", "/F",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect(),
        vec!["/Run", "/TN", task]
            .into_iter()
            .map(str::to_owned)
            .collect(),
    ]
}
#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn package_windows_scheduler_survives_transport_exit() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let shim = dir.join("schtasks");
        std::fs::write(&shim,r#"#!/bin/sh
set -eu
case "$1" in
/Create) test "$2" = /TN; test "$4" = /TR; test "$6" = /SC; test "$7" = ONCE; printf '%s' "$5" > "$FIXTURE/action" ;;
/Run) test -f "$FIXTURE/action"; setsid sh -c 'echo $$ > "$FIXTURE/pid"; sleep 0.2; echo done > "$FIXTURE/done"' </dev/null >/dev/null 2>&1 & ;;
*) exit 1;;
esac
"#).unwrap();
        std::fs::set_permissions(&shim, std::fs::Permissions::from_mode(0o755)).unwrap();
        let cmds = commands("fixture", "C:\\tmp\\package runner.ps1");
        assert_eq!(cmds.len(), 2);
        for cmd in cmds {
            assert!(std::process::Command::new(&shim)
                .args(cmd)
                .env("FIXTURE", &dir)
                .status()
                .unwrap()
                .success());
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while !dir.join("done").exists() && std::time::Instant::now() < deadline {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert!(dir.join("pid").exists());
        assert!(dir.join("done").exists());
        assert!(std::fs::read_to_string(dir.join("action"))
            .unwrap()
            .contains("package runner.ps1"));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
