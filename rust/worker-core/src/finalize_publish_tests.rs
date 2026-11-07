use super::*;
use std::cell::Cell;
struct FakePr(Cell<usize>);
impl PrStep for FakePr {
    fn find_or_update(&self, _: &str, _: &str, _: &str, _: &str) -> Result<PrRef> {
        self.0.set(self.0.get() + 1);
        Ok(PrRef { number: 42 })
    }
}
struct Fixture {
    dir: tempfile::TempDir,
    head: String,
    old: String,
    remote_path: String,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let remote_path = dir.path().join("remote.git").to_str().unwrap().to_owned();
        let remote = Remote {
            root: dir.path(),
            name: &remote_path,
        };
        assert!(git(&remote, &["init", "--bare", "remote.git"])
            .unwrap()
            .status
            .success());
        assert!(git(&remote, &["init", "."]).unwrap().status.success());
        let commit = |parent: Option<&str>| {
            let tree = git(&remote, &["mktree"]).unwrap();
            assert!(tree.status.success());
            let tree = std::str::from_utf8(&tree.stdout).unwrap().trim();
            let mut args = vec![
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit-tree",
                tree,
                "-m",
                "test",
            ];
            if let Some(parent) = parent {
                args.extend(["-p", parent]);
            }
            let output = git(&remote, &args).unwrap();
            assert!(output.status.success());
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        };
        let old = commit(None);
        let head = commit(Some(&old));
        Self {
            dir,
            head,
            old,
            remote_path,
        }
    }
    fn remote(&self) -> Remote<'_> {
        Remote {
            root: self.dir.path(),
            name: &self.remote_path,
        }
    }
    fn log(&self) -> LocalFileStepLog {
        LocalFileStepLog {
            dir: self.dir.path().join("logs"),
        }
    }
    fn run(&self, pr: &FakePr) -> Outcome {
        finalize_publish(
            "run",
            &self.head,
            "lane",
            None,
            &self.remote(),
            pr,
            &self.log(),
        )
        .unwrap()
    }
}
#[test]
fn remote_allowlist_rejects_helpers_and_other_transports() {
    let f = Fixture::new();
    for name in [
        "",
        "-x",
        "ext::sh -c evil",
        "fd::pipe",
        "https://host/qq::zz",
        "file:///tmp/r",
        "http://host/r",
        "ftp://host/r",
        "relative/path",
        "https:///r",
        "/tmp/a\n",
    ] {
        assert!(
            matches!(
                publish(
                    &f.head,
                    "lane",
                    None,
                    &Remote {
                        root: f.dir.path(),
                        name
                    }
                ),
                Err(PublishError::Invalid)
            ),
            "{name}"
        );
    }
    for name in [
        "/tmp/remote.git",
        "https://host/repo.git",
        "ssh://git@host/repo.git",
        "git@host:repo.git",
    ] {
        assert!(valid_remote(name), "{name}");
    }
}
#[test]
fn inherited_git_overrides_are_removed() {
    let env = [
        ("GIT_CONFIG_COUNT", "1"),
        ("GIT_CONFIG_KEY_0", "alias.injected"),
        ("GIT_CONFIG_VALUE_0", "bad"),
        ("GIT_CONFIG_KEY_17", "bad"),
        ("GIT_CONFIG_VALUE_17", "bad"),
        ("GIT_CONFIG_PARAMETERS", "malformed"),
        ("GIT_DIR", "/nonexistent"),
        ("GIT_WORK_TREE", "/nonexistent"),
        ("GIT_OBJECT_DIRECTORY", "/nonexistent"),
        ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/nonexistent"),
        ("GIT_INDEX_FILE", "/nonexistent"),
        ("GIT_EXEC_PATH", "/nonexistent"),
        ("GIT_SSH_COMMAND", "untrusted-command"),
        ("GIT_SSH", "/nonexistent"),
        ("GIT_COMMON_DIR", "/nonexistent"),
        ("GIT_NAMESPACE", "untrusted"),
        ("GIT_FOO", "untrusted"),
        ("GIT_ASKPASS", "/nonexistent"),
        ("SSH_ASKPASS", "/nonexistent"),
        ("SSH_FOO", "untrusted"),
    ];
    // Re-execute only this test so injected parent variables cannot race other tests.
    const MARKER: &str = "PUBLISH_ENV_TEST_CHILD";
    if std::env::var_os(MARKER).is_none() {
        let out = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                concat!(module_path!(), "::inherited_git_overrides_are_removed")
                    .split_once("::")
                    .unwrap()
                    .1,
            ])
            .env(MARKER, "1")
            .envs(env)
            .output()
            .unwrap();
        let report = String::from_utf8(out.stdout).unwrap();
        assert!(report.contains("running 1 test"), "{report}");
        assert!(report.contains("test result: ok. 1 passed;"), "{report}");
        assert!(out.status.success(), "{report}");
        return;
    }
    let f = Fixture::new();
    let out = git_with_env(&f.remote(), &["--test-env-probe"], &[]).unwrap();
    assert!(out.status.success());
    let child_env = String::from_utf8(out.stdout).unwrap();
    for (key, _) in env {
        assert!(
            !child_env
                .lines()
                .any(|line| line.starts_with(&format!("{key}="))),
            "{key}"
        );
    }
    assert!(child_env
        .lines()
        .any(|line| line == "GIT_TERMINAL_PROMPT=0"));
    let out = git_with_env(
        &f.remote(),
        &["--test-env-probe"],
        &[
            ("GIT_DIR", "/nonexistent"),
            ("GIT_SSH_COMMAND", "untrusted-command"),
            ("SSH_ASKPASS", "/nonexistent"),
            ("GIT_FOO", "explicit"),
            ("SSH_FOO", "explicit"),
            ("PUBLISH_SAFE_ENV", "explicit"),
            ("GIT_TERMINAL_PROMPT", "1"),
        ],
    )
    .unwrap();
    assert!(out.status.success());
    let child_env = String::from_utf8(out.stdout).unwrap();
    for line in child_env.lines() {
        let (key, _) = line.split_once('=').unwrap();
        assert!(
            !(key.starts_with("GIT_") || key.starts_with("SSH_"))
                || GIT_FIXED_ENV.iter().any(|(fixed, _)| *fixed == key),
            "unexpected child environment key {key}; only helper-set GIT_FIXED_ENV keys are allowed"
        );
    }
    for (key, value) in GIT_FIXED_ENV {
        assert!(child_env
            .lines()
            .any(|line| line == format!("{key}={value}")));
    }
    assert!(child_env
        .lines()
        .any(|line| line == "PUBLISH_SAFE_ENV=explicit"));
    let out = git(&f.remote(), &["config", "--get", "alias.injected"]).unwrap();
    assert_eq!(out.status.code(), Some(1));
    let out = git(&f.remote(), &["rev-parse", "--git-dir"]).unwrap();
    assert!(out.status.success());
    assert_eq!(String::from_utf8(out.stdout).unwrap().trim(), ".git");
    let out = git(&f.remote(), &["cat-file", "-t", &f.head]).unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout, b"commit\n");
    assert!(git(&f.remote(), &["read-tree", &f.head])
        .unwrap()
        .status
        .success());
    assert!(f.dir.path().join(".git/index").exists());
}
#[test]
fn replay_input_drift_is_invalid() {
    let f = Fixture::new();
    f.run(&FakePr(Cell::new(0)));
    for (head, old) in [(&f.old, None), (&f.head, Some(f.old.as_str()))] {
        assert!(matches!(
            finalize_publish(
                "run",
                head,
                "lane",
                old,
                &f.remote(),
                &FakePr(Cell::new(0)),
                &f.log()
            ),
            Err(PublishError::Invalid)
        ));
    }
}
#[test]
fn corrupt_interior_line_reports_number() {
    let f = Fixture::new();
    let log = f.log();
    log.append("run", Step::Done).unwrap();
    OpenOptions::new()
        .append(true)
        .open(log.path("run").unwrap())
        .unwrap()
        .write_all(b"not-json\n\"Done\"\n")
        .unwrap();
    let error = log.load("run").unwrap_err();
    assert!(matches!(error, PublishError::LogLine(2)));
    assert_eq!(error.to_string(), "invalid_step_log_line_2");
}
#[test]
fn fixture_has_explicit_isolated_ident() {
    let f = Fixture::new();
    let out = git(&f.remote(), &["show", "-s", "--format=%an <%ae>", &f.head]).unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout, b"Fixture <fixture@example.invalid>\n");
}
#[test]
fn expected_absent_and_already_published() {
    let f = Fixture::new();
    assert_eq!(
        publish(&f.head, "lane", None, &f.remote()).unwrap(),
        Outcome::Published
    );
    assert_eq!(
        publish(&f.head, "lane", None, &f.remote()).unwrap(),
        Outcome::AlreadyPublished
    );
}
#[test]
fn expected_old_match() {
    let f = Fixture::new();
    publish(&f.old, "lane", None, &f.remote()).unwrap();
    assert_eq!(
        publish(&f.head, "lane", Some(&f.old), &f.remote()).unwrap(),
        Outcome::Published
    );
}
#[test]
fn moved_lane_conflict_is_terminal() {
    let f = Fixture::new();
    publish(&f.old, "lane", None, &f.remote()).unwrap();
    let pr = FakePr(Cell::new(0));
    assert_eq!(f.run(&pr), Outcome::Conflict);
    assert_eq!(f.run(&pr), Outcome::Conflict);
    assert_eq!(pr.0.get(), 0);
    assert_eq!(
        lane_head(&f.remote(), "refs/heads/lane").unwrap(),
        Some(f.old.clone())
    );
    assert_eq!(f.log().load("run").unwrap().len(), 1);
}
#[test]
fn recovery_after_cas_and_done_skips_calls() {
    let f = Fixture::new();
    publish(&f.head, "lane", None, &f.remote()).unwrap();
    f.log()
        .append(
            "run",
            Step::LaneCas {
                head: f.head.clone(),
                expected_old: None,
                outcome: Outcome::Published,
            },
        )
        .unwrap();
    // Remove the remote entirely: recovery must not perform even a Git read.
    fs::rename(
        f.dir.path().join("remote.git"),
        f.dir.path().join("offline.git"),
    )
    .unwrap();
    let pr = FakePr(Cell::new(0));
    assert_eq!(f.run(&pr), Outcome::Published);
    assert_eq!(pr.0.get(), 1);
    assert_eq!(f.run(&pr), Outcome::Published);
    assert_eq!(pr.0.get(), 1);
    assert_eq!(f.log().load("run").unwrap().len(), 3);
}
#[test]
fn torn_line_is_ignored_and_repaired() {
    let f = Fixture::new();
    let log = f.log();
    log.append("run", Step::Pr { number: 1 }).unwrap();
    OpenOptions::new()
        .append(true)
        .open(log.path("run").unwrap())
        .unwrap()
        .write_all(b"{torn")
        .unwrap();
    assert_eq!(log.load("run").unwrap(), vec![Step::Pr { number: 1 }]);
    log.append("run", Step::Done).unwrap();
    assert_eq!(
        log.load("run").unwrap(),
        vec![Step::Pr { number: 1 }, Step::Done]
    );
}
#[test]
fn bad_run_id_refused() {
    let f = Fixture::new();
    for id in ["", "../escape", "a/b", ".", "a\n"] {
        assert!(f.log().load(id).is_err());
        assert!(f.log().append(id, Step::Done).is_err());
    }
}

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
