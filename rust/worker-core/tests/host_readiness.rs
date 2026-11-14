use pg_worker_core::host_readiness::{check, Config, Probe, Runner};
use std::{io, path::Path};

struct Fake {
    fail: Option<Probe>,
    calls: Vec<Probe>,
}
impl Runner for Fake {
    fn probe(&mut self, probe: Probe, _: &Path, _: &str) -> io::Result<()> {
        self.calls.push(probe);
        if self.fail == Some(probe) {
            Err(io::Error::other("injected failure"))
        } else {
            Ok(())
        }
    }
}
fn config() -> Config {
    Config {
        sandbox: "workspace-write".into(),
        project_trusted: true,
        hooks_trusted: true,
        update_prompt_disabled: true,
        mem_available_mb: Some(16000),
        min_mem_mb: 3000,
    }
}
#[test]
fn prepared_host_ready_without_a_model_call() {
    let tmp = tempfile::tempdir().unwrap();
    let mut runner = Fake {
        fail: None,
        calls: vec![],
    };
    assert!(check(&config(), tmp.path(), &mut runner).is_empty());
    assert_eq!(
        runner.calls,
        [Probe::Version, Probe::Login, Probe::Sandbox, Probe::Commit]
    );
}
#[test]
fn reproduces_sandbox_start_and_git_read_only_failures() {
    let tmp = tempfile::tempdir().unwrap();
    for probe in [Probe::Sandbox, Probe::Commit] {
        let mut runner = Fake {
            fail: Some(probe),
            calls: vec![],
        };
        let errors = check(&config(), tmp.path(), &mut runner);
        assert_eq!(
            errors,
            [if probe == Probe::Sandbox {
                "sandbox cannot start"
            } else {
                "scratch worktree commit failed"
            }]
        );
    }
}
#[test]
fn prompts_login_version_and_memory_are_independent_failures() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cfg = config();
    cfg.project_trusted = false;
    cfg.hooks_trusted = false;
    cfg.update_prompt_disabled = false;
    cfg.mem_available_mb = None;
    let mut runner = Fake {
        fail: Some(Probe::Login),
        calls: vec![],
    };
    let errors = check(&cfg, tmp.path(), &mut runner);
    assert_eq!(errors.len(), 5);
    assert!(errors.contains(&"codex login unavailable".into()));
    runner.fail = Some(Probe::Version);
    assert!(check(&config(), tmp.path(), &mut runner).contains(&"codex version unavailable".into()));
}

#[test]
fn reads_trust_and_update_settings_without_executing_hooks() {
    let tmp = tempfile::tempdir().unwrap();
    let project = tempfile::tempdir().unwrap();
    let quoted =
        serde_json::to_string(&project.path().canonicalize().unwrap().to_string_lossy()).unwrap();
    std::fs::write(
        tmp.path().join("config.toml"),
        format!(
            "check_for_update_on_startup = false\n[projects.{quoted}]\ntrust_level = 'trusted'\n"
        ),
    )
    .unwrap();
    let cfg = pg_worker_core::host_readiness::settings(
        tmp.path(),
        project.path(),
        "workspace-write".into(),
    )
    .unwrap();
    assert!(cfg.project_trusted && cfg.update_prompt_disabled && cfg.hooks_trusted);
    std::fs::write(tmp.path().join("hooks.json"), "{}").unwrap();
    assert!(
        !pg_worker_core::host_readiness::settings(
            tmp.path(),
            project.path(),
            "workspace-write".into()
        )
        .unwrap()
        .hooks_trusted
    );
}

#[test]
fn real_commit_child_writes_only_temporary_git_metadata() {
    let tmp = tempfile::tempdir().unwrap();
    let mut cmd = std::process::Command::new("git");
    cmd.current_dir(tmp.path()).args(["init", "-q"]);
    pg_worker_core::host_command::output(&mut cmd, std::time::Duration::from_secs(10)).unwrap();
    pg_worker_core::host_readiness::commit_child(tmp.path()).unwrap();
    assert!(tmp.path().join("ready.txt").is_file());
}
