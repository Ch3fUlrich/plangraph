use super::apply::{run, Host, Outcome, Steps};
use super::report::LocalFacts;
use super::systemd::{Out, Systemctl};
use super::*;
use std::cell::{Cell, RefCell};
use std::fs;
use std::path::PathBuf;

const MANIFEST: &str = r#"
[[unit]]
name = "pg-a"
files = ["pg-a.service", "pg-a.timer"]
hosts = ["all"]
env_files = ["x.env"]
env_family = "fx"
first_run = "ok"

[[unit]]
name = "pg-b"
files = ["pg-b.service"]
hosts = ["all"]
first_run = "ok"

[[binary]]
package = "p"
bin = "tool"
hosts = ["all"]
first_run = "tool --version"
"#;

/// A host with its own tiny systemd: a unit is loaded when its file is in the unit directory, enabled and
/// active after `enable --now`.
struct FakeSystemd {
    unit_dir: PathBuf,
    enabled: RefCell<BTreeSet<String>>,
    /// Services that have run once: a timer starts its service only when it fires, not when it is enabled.
    fired: RefCell<std::collections::BTreeMap<String, i64>>,
    /// When each enabled unit last (re)started: `enable --now` and `restart` set it.
    entered: RefCell<std::collections::BTreeMap<String, i64>>,
    restarts: RefCell<Vec<String>>,
    restart_fails: Cell<bool>,
    reload_fails: Cell<bool>,
    /// What systemd has loaded: the unit file texts as of its last daemon-reload.
    loaded_text: RefCell<std::collections::BTreeMap<String, Vec<u8>>>,
}

impl Systemctl for FakeSystemd {
    fn run(&self, _: Scope, args: &[&str]) -> Result<Out, String> {
        let ok = |stdout: String| {
            Ok(Out {
                code: 0,
                stdout,
                stderr: String::new(),
            })
        };
        let args: Vec<&str> = args
            .iter()
            .copied()
            .filter(|a| *a != "--timestamp=unix")
            .collect();
        let at = |t: Option<i64>| t.map_or(String::new(), |t| format!("@{t}"));
        match args.as_slice() {
            ["show", unit, ..] => {
                let loaded = self.unit_dir.join(unit).is_file();
                let on = self.enabled.borrow().contains(*unit);
                let ran = self.fired.borrow().get(*unit).copied();
                ok(format!(
                    "LoadState={}\nActiveState={}\nUnitFileState={}\nResult=success\nNeedDaemonReload={}\nExecMainStartTimestamp={}\nExecMainStatus={}\nActiveEnterTimestamp={}\n",
                    if loaded { "loaded" } else { "not-found" },
                    if on { "active" } else { "inactive" },
                    if on { "enabled" } else { "disabled" },
                    if loaded && self.loaded_text.borrow().get(*unit) != fs::read(self.unit_dir.join(unit)).ok().as_ref() { "yes" } else { "no" },
                    at(ran),
                    if ran.is_some() { "0" } else { "" },
                    at(self.entered.borrow().get(*unit).copied())
                ))
            }
            ["is-enabled", unit] => ok(if self.enabled.borrow().contains(*unit) {
                "enabled\n".into()
            } else {
                "disabled\n".into()
            }),
            ["is-active", unit] => ok(if self.enabled.borrow().contains(*unit) {
                "active\n".into()
            } else {
                "inactive\n".into()
            }),
            ["enable", "--now", unit] => {
                self.enabled.borrow_mut().insert((*unit).to_owned());
                self.entered
                    .borrow_mut()
                    .insert((*unit).to_owned(), clock());
                ok(String::new())
            }
            ["restart", _] if self.restart_fails.get() => Ok(Out {
                code: 1,
                stdout: String::new(),
                stderr: "Job failed".into(),
            }),
            ["restart", unit] => {
                self.restarts.borrow_mut().push((*unit).to_owned());
                self.entered
                    .borrow_mut()
                    .insert((*unit).to_owned(), clock());
                ok(String::new())
            }
            ["daemon-reload"] if self.reload_fails.get() => Ok(Out {
                code: 1,
                stdout: String::new(),
                stderr: "Failed to connect to bus".into(),
            }),
            ["daemon-reload"] => {
                let mut loaded = self.loaded_text.borrow_mut();
                for entry in fs::read_dir(&self.unit_dir).into_iter().flatten().flatten() {
                    loaded.insert(
                        entry.file_name().to_string_lossy().into_owned(),
                        fs::read(entry.path()).unwrap_or_default(),
                    );
                }
                ok(String::new())
            }
            _ => ok(String::new()),
        }
    }
}

/// The host clock, seconds: file times and unit start times come from the same clock.
fn clock() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}

struct FakeSteps {
    repo: PathBuf,
    bin_dir: PathBuf,
    state_dir: PathBuf,
    config_dir: PathBuf,
    releases: Cell<u32>,
    families: RefCell<Vec<String>>,
}

impl Steps for FakeSteps {
    fn release(&self) -> Result<(), String> {
        self.releases.set(self.releases.get() + 1);
        fs::create_dir_all(&self.bin_dir).map_err(|e| e.to_string())?;
        let tool = self.bin_dir.join("tool");
        fs::write(&tool, "#!/bin/sh\n").map_err(|e| e.to_string())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&tool, fs::Permissions::from_mode(0o755))
                .map_err(|e| e.to_string())?;
        }
        fs::create_dir_all(&self.state_dir).map_err(|e| e.to_string())?;
        let (stamp, _) = super::stamp::release_stamp(&self.repo)?;
        let sha = super::stamp::sha256_file(&tool).unwrap();
        super::stamp::record_artifact(&self.state_dir, "tool", &stamp, &sha)
            .map_err(|e| e.to_string())?;
        fs::write(
            self.state_dir.join("release-built-tree"),
            format!("{stamp}\n"),
        )
        .map_err(|e| e.to_string())
    }

    fn env_family(&self, family: &str) -> Result<(), String> {
        self.families.borrow_mut().push(family.into());
        fs::create_dir_all(&self.config_dir).map_err(|e| e.to_string())?;
        fs::write(self.config_dir.join("x.env"), "A=1\n").map_err(|e| e.to_string())
    }
}

struct World {
    dir: tempfile::TempDir,
    manifest: Manifest,
    systemd: FakeSystemd,
    steps: FakeSteps,
}

fn world(text: &str) -> World {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let text = &text.replace("@ROOT@", &root.display().to_string());
    fs::create_dir_all(root.join("repo/deploy/systemd")).unwrap();
    for file in [
        "pg-a.service",
        "pg-a.timer",
        "pg-b.service",
        "pg-sys.service",
        "pg-sys.timer",
    ] {
        fs::write(
            root.join("repo/deploy/systemd").join(file),
            format!("[Unit]\n# {file}\n"),
        )
        .unwrap();
    }
    fs::write(root.join("repo/deploy/manifest.toml"), text).unwrap();
    // The checkout is a git repository: the release stamp is its Rust tree.
    fs::create_dir_all(root.join("repo/rust")).unwrap();
    fs::write(root.join("repo/rust/x.rs"), "fn main() {}\n").unwrap();
    for args in [
        vec!["init", "-q"],
        vec!["add", "-A"],
        vec!["commit", "-qm", "one"],
    ] {
        let out = std::process::Command::new("git")
            .arg("-C")
            .arg(root.join("repo"))
            .args(["-c", "user.name=F", "-c", "user.email=f@example.invalid"])
            .args(args)
            .output()
            .unwrap();
        assert!(out.status.success(), "{out:?}");
    }
    let systemd = FakeSystemd {
        unit_dir: root.join("units"),
        enabled: RefCell::default(),
        fired: RefCell::default(),
        entered: RefCell::default(),
        restarts: RefCell::default(),
        restart_fails: Cell::new(false),
        reload_fails: Cell::new(false),
        loaded_text: RefCell::default(),
    };
    let steps = FakeSteps {
        repo: root.join("repo"),
        bin_dir: root.join("bin"),
        state_dir: root.join("state"),
        config_dir: root.join("config"),
        releases: Cell::new(0),
        families: RefCell::default(),
    };
    World {
        manifest: Manifest::parse(text).unwrap(),
        dir,
        systemd,
        steps,
    }
}

/// The timer fires: its service runs once and succeeds.
fn fire(w: &World, service: &str) {
    // After the unit file was written (file times are whole seconds).
    w.systemd
        .fired
        .borrow_mut()
        .insert(service.to_owned(), clock() + 10);
}

fn install(w: &World, check_only: bool) -> Outcome {
    let root = w.dir.path();
    let host = Host {
        repo: &root.join("repo"),
        unit_dir: &root.join("units"),
        config_dir: &root.join("config"),
        ledger_path: Some(root.join("state/deploy-live/h.json")),
    };
    let facts = LocalFacts {
        repo: root.join("repo"),
        bin_dir: root.join("bin"),
        state_dir: root.join("state"),
        unit_dir: root.join("units"),
    };
    run(
        &w.manifest,
        &BTreeSet::new(),
        &host,
        &w.systemd,
        &facts,
        &w.steps,
        check_only,
    )
    .unwrap()
}

#[test]
fn a_host_missing_a_binary_a_unit_and_an_env_file_gets_a_plan_naming_all_three() {
    let w = world(MANIFEST);
    let out = install(&w, true);
    let text = out.lines.join("\n");
    assert!(
        text.contains("plan\tbinary\tp/tool\tnot-installed\tinstall release binaries"),
        "{text}"
    );
    assert!(
        text.contains("plan\tunit\tpg-a\tenv file missing: x.env\tcreate env files (family fx)"),
        "{text}"
    );
    assert!(text.contains("pg-a.service missing"), "{text}");
}

#[test]
fn check_only_changes_nothing_and_reports_pending() {
    let w = world(MANIFEST);
    let out = install(&w, true);
    assert!(out.pending);
    assert_eq!(w.steps.releases.get(), 0);
    assert!(!w.dir.path().join("units").exists() && !w.dir.path().join("config").exists());
}

#[test]
fn the_install_fixes_all_three_and_a_second_run_changes_nothing() {
    let w = world(MANIFEST);
    let first = install(&w, false);
    let text = first.lines.join("\n");
    // Installed and enabled, but the timer has not fired yet: nothing shows the service works.
    assert!(first.pending, "{text}");
    assert!(
        text.contains("still: never-run(pg-a.service has not run since it was installed)"),
        "{text}"
    );
    for row in [
        "result\tbinary\tp/tool\tnot-installed\tok",
        "result\tunit\tpg-a\tenv file missing: x.env\tok",
    ] {
        assert!(text.contains(row), "{row} in\n{text}");
    }
    assert!(
        w.dir.path().join("units/pg-a.timer").is_file() && w.dir.path().join("bin/tool").is_file()
    );
    assert!(w.systemd.enabled.borrow().contains("pg-a.timer"));
    assert!(text.contains("deploy-not-live\t1"), "{text}");
    fire(&w, "pg-a.service");

    let second = install(&w, false);
    assert!(!second.pending);
    assert!(
        second.lines.join("\n").contains("nothing to do\t"),
        "{:?}",
        second.lines
    );
    assert_eq!(
        (w.steps.releases.get(), w.steps.families.borrow().len()),
        (1, 1),
        "no step ran twice"
    );
    assert!(
        !install(&w, true).pending,
        "--check exits clean after the install"
    );
    let ledger = fs::read_to_string(w.dir.path().join("state/deploy-live/h.json")).unwrap();
    assert!(ledger.contains("\"not_live_since\": {}"), "{ledger}");
}

#[test]
fn an_outdated_unit_file_is_replanned_and_repaired() {
    let w = world(MANIFEST);
    install(&w, false);
    fire(&w, "pg-a.service");
    fs::write(
        w.dir.path().join("repo/deploy/systemd/pg-a.timer"),
        "[Unit]\n# newer\n",
    )
    .unwrap();
    let check = install(&w, true);
    assert!(
        check.pending && check.lines.join("\n").contains("pg-a.timer outdated"),
        "{:?}",
        check.lines
    );
    assert!(!install(&w, false).pending);
    assert!(fs::read_to_string(w.dir.path().join("units/pg-a.timer"))
        .unwrap()
        .contains("newer"));
}

#[test]
fn a_secret_env_file_is_named_for_the_operator_and_the_unit_is_not_enabled() {
    let w = world(
        &MANIFEST
            .replace("env_family = \"fx\"\n", "")
            .replace("x.env", "secret.env"),
    );
    let out = install(&w, false);
    let text = out.lines.join("\n");
    assert!(out.pending, "{text}");
    assert!(text.contains("OPERATOR: provide secret.env"), "{text}");
    assert!(text.contains("NEEDS-ENV pg-a: secret.env"), "{text}");
    assert!(!w.systemd.enabled.borrow().contains("pg-a.timer"));
    assert!(
        !w.dir.path().join("config/secret.env").exists(),
        "a secret file is never created"
    );
}

#[test]
fn a_root_unit_is_listed_as_an_operator_step_and_never_installed() {
    let w = world(&format!(
        "{MANIFEST}\n[[unit]]\nname = \"pg-sys\"\nfiles = [\"pg-sys.service\", \"pg-sys.timer\"]\nscope = \"system\"\nhosts = [\"all\"]\nfirst_run = \"ok\"\n"
    ));
    let out = install(&w, false);
    let text = out.lines.join("\n");
    assert!(
        text.contains("plan\tunit\tpg-sys\t") && text.contains("OPERATOR: install as root"),
        "{text}"
    );
    assert!(out.pending);
    assert!(!w.dir.path().join("units/pg-sys.timer").exists());
}

#[test]
fn a_failed_daemon_reload_leaves_the_run_pending_and_the_retry_reloads() {
    let w = world(MANIFEST);
    install(&w, false);
    fire(&w, "pg-a.service");
    assert!(!install(&w, false).pending);
    // The checkout changes the timer; the copy succeeds, the reload fails.
    fs::write(
        w.dir.path().join("repo/deploy/systemd/pg-a.timer"),
        "[Unit]\n# newer\n",
    )
    .unwrap();
    w.systemd.reload_fails.set(true);
    let failed = install(&w, false);
    let text = failed.lines.join("\n");
    assert!(
        failed.pending,
        "the installer's own failure keeps the run pending: {text}"
    );
    assert!(text.contains("FAIL pg-a: daemon-reload"), "{text}");
    // The file now equals the checkout: only systemd's NeedDaemonReload still shows the work.
    assert_eq!(
        fs::read(w.dir.path().join("units/pg-a.timer")).unwrap(),
        fs::read(w.dir.path().join("repo/deploy/systemd/pg-a.timer")).unwrap()
    );
    let check = install(&w, true);
    assert!(check.pending, "{:?}", check.lines);
    assert!(check.lines.join("\n").contains("needs-daemon-reload"));
    w.systemd.reload_fails.set(false);
    assert!(
        !install(&w, false).pending,
        "the retry reloads and ends clean"
    );
}

#[test]
fn an_updated_active_service_is_restarted_so_the_running_process_is_the_new_revision() {
    let w = world(MANIFEST);
    install(&w, false);
    fire(&w, "pg-a.service");
    assert!(!install(&w, false).pending);
    assert!(
        w.systemd.restarts.borrow().is_empty(),
        "nothing changed: nothing restarted"
    );
    // The checkout ships a new text for the long-running service; copy and reload alone leave the old
    // process in charge (its start predates the written file).
    fs::write(
        w.dir.path().join("repo/deploy/systemd/pg-b.service"),
        "[Unit]\n# a new revision\n",
    )
    .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(1100));
    let out = install(&w, false);
    let text = out.lines.join("\n");
    assert!(text.contains("RESTARTED pg-b: pg-b.service"), "{text}");
    assert_eq!(*w.systemd.restarts.borrow(), ["pg-b.service"]);
    assert!(!out.pending, "{text}");
    assert!(
        !install(&w, true).pending,
        "the restarted unit is the new revision"
    );
}

#[test]
fn an_updated_service_that_cannot_be_restarted_stays_pending_instead_of_reporting_success() {
    let w = world(MANIFEST);
    install(&w, false);
    fire(&w, "pg-a.service");
    fs::write(
        w.dir.path().join("repo/deploy/systemd/pg-b.service"),
        "[Unit]\n# a new revision\n",
    )
    .unwrap();
    std::thread::sleep(std::time::Duration::from_millis(1100));
    w.systemd.restart_fails.set(true);
    let out = install(&w, false);
    assert!(out.pending, "{:?}", out.lines);
    assert!(out
        .lines
        .join("\n")
        .contains("FAIL pg-b: restart pg-b.service"));
    // The file already matches the checkout, yet the unit still runs the old revision: still listed.
    let check = install(&w, true);
    assert!(
        check.pending
            && check
                .lines
                .join("\n")
                .contains("runs a revision older than its installed file")
    );
    w.systemd.restart_fails.set(false);
}

#[test]
fn a_binary_a_system_unit_runs_from_another_path_is_live_only_when_the_operator_copied_the_artifact_there(
) {
    let w = world(
        r#"
[[binary]]
package = "p"
bin = "tool"
install_path = "@ROOT@/system/tool"
hosts = ["all"]
first_run = "x"
"#,
    );
    let first = install(&w, false);
    let text = first.lines.join(
        "
",
    );
    // The release built and installed it under the user bin directory; the consumer's path is still empty.
    assert!(first.pending, "{text}");
    assert!(w.dir.path().join("bin/tool").is_file());
    let after = install(&w, true);
    assert!(
        after
            .lines
            .join(
                "
"
            )
            .contains("OPERATOR: copy the built binary to"),
        "{:?}",
        after.lines
    );
    // A different file at the consumer's path is not the artifact either.
    fs::create_dir_all(w.dir.path().join("system")).unwrap();
    let place = |from: &str| {
        let to = w.dir.path().join("system/tool");
        fs::write(&to, from).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&to, fs::Permissions::from_mode(0o755)).unwrap();
        }
    };
    place("an older build\n");
    assert!(install(&w, true)
        .lines
        .join("\n")
        .contains("is not the artifact built for this release"));
    place("#!/bin/sh\n");
    assert!(!install(&w, true).pending, "{:?}", install(&w, true).lines);
}
