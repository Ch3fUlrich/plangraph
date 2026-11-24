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
        match args {
            ["show", unit, ..] => {
                let loaded = self.unit_dir.join(unit).is_file();
                let on = self.enabled.borrow().contains(*unit);
                ok(format!(
                    "LoadState={}\nActiveState={}\nUnitFileState={}\nResult=success\n",
                    if loaded { "loaded" } else { "not-found" },
                    if on { "active" } else { "inactive" },
                    if on { "enabled" } else { "disabled" }
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
                ok(String::new())
            }
            _ => ok(String::new()),
        }
    }
}

struct FakeSteps {
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
        fs::write(self.state_dir.join("release-built-tree"), "tree1\n").map_err(|e| e.to_string())
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
    fs::create_dir_all(root.join("repo/deploy/systemd")).unwrap();
    for file in [
        "pg-a.service",
        "pg-a.timer",
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
    let systemd = FakeSystemd {
        unit_dir: root.join("units"),
        enabled: RefCell::default(),
    };
    let steps = FakeSteps {
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
    assert!(!first.pending, "{text}");
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
    assert!(text.contains("deploy-not-live\t0"), "{text}");
    let ledger = fs::read_to_string(w.dir.path().join("state/deploy-live/h.json")).unwrap();
    assert!(ledger.contains("\"not_live_since\": {}"), "{ledger}");

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
}

#[test]
fn an_outdated_unit_file_is_replanned_and_repaired() {
    let w = world(MANIFEST);
    install(&w, false);
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
