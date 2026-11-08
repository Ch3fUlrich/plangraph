#[cfg(unix)]
use super::*;

// Unix-only: this transport probe needs a POSIX shell and executable mode bits.
#[cfg(unix)]
#[rustfmt::skip]
#[test]
fn ssh_agent_socket_reaches_transport_only() {
    const MARKER: &str = "PUBLISH_SOCKET_CHILD";
    if std::env::var_os(MARKER).is_none() {
        for set in [false, true] {
            let mut child = std::process::Command::new(std::env::current_exe().unwrap());
            child.args(["--exact", concat!(module_path!(), "::ssh_agent_socket_reaches_transport_only").split_once("::").unwrap().1])
                .env(MARKER, "1").env_remove("GIT_SSH_COMMAND");
            if set { child.env("SSH_AUTH_SOCK", "/fixture/agent"); }
            else { child.env_remove("SSH_AUTH_SOCK"); }
            let out = child.output().unwrap();
            let report = String::from_utf8(out.stdout).unwrap();
            assert!(report.contains("running 1 test"), "{report}");
            assert!(report.contains("test result: ok. 1 passed;"), "{report}");
            assert!(out.status.success(), "{report}");
        }
        return;
    }
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(GIT_PASSTHROUGH_ENV, &["SSH_AUTH_SOCK"]);
    let f = Fixture::new();
    let script = f.dir.path().join("ssh-probe");
    let record = f.dir.path().join("socket-record");
    fs::write(&script, format!("#!/bin/sh\nprintf '%s' \"${{SSH_AUTH_SOCK-unset}}\" > '{}'\nexit 1\n", record.display())).unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
    assert!(git(&f.remote(), &["config", "core.sshCommand", script.to_str().unwrap()]).unwrap().status.success());
    let remote = Remote { root: f.dir.path(), name: "git@host:repo.git" };
    for args in [vec!["ls-remote", remote.name], vec!["fetch", remote.name], vec!["push", remote.name, &f.head]] {
        let _ = git(&remote, &args).unwrap();
        assert_eq!(fs::read_to_string(&record).unwrap(), std::env::var("SSH_AUTH_SOCK").unwrap_or_else(|_| "unset".into()));
    }
}
