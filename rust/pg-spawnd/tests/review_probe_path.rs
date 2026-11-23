#![cfg(target_os = "linux")]

use std::{fs, os::unix::fs::symlink, path::Path, process::Command};

const UNIT: &str = include_str!("../../../deploy/systemd/plangraph-review-probe.service");
const MANAGER_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
const FIXTURE: &str = r#"
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plangraph.planner import legs
from plangraph.routing_support.leg_state import LegStateStore
from plangraph.spawner import cli, leg_probe

state_dir = Path(sys.argv[sys.argv.index('--state') + 1])
store = LegStateStore(state_dir / 'legs.sqlite')
names = ('codex', 'opencode', 'claude', 'agy')
selected = [replace(legs.DEFAULT_LEGS[0], id=name, harness=name, model=name)
            for name in names]

class Service:
    def __init__(self, **kwargs):
        self.runner = self
        self.leg_reader = store

    async def spawn(self, **kwargs):
        return SimpleNamespace(job_id=kwargs['client'], status='failed', exit_code=17)

    async def cancel(self, job_id):
        pass

    def close(self):
        pass

with (patch.object(legs, 'routed_review_legs', return_value=selected),
      patch.object(cli, 'SpawnService', Service),
      patch.object(leg_probe, '_check_manage_key', return_value=None)):
    sys.exit(cli.main(sys.argv[1:]))
"#;

fn probe(home: &Path, path: &str, fixture_legs: bool) -> (Option<i32>, serde_json::Value) {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let entry = UNIT
        .lines()
        .find_map(|line| line.strip_prefix("ExecStart="))
        .unwrap();
    let mut argv = entry.split_whitespace();
    let python = argv.next().unwrap();
    assert_eq!(
        argv.by_ref().take(2).collect::<Vec<_>>(),
        ["-m", "plangraph.spawner.cli"]
    );
    let mut command = Command::new(python);
    if fixture_legs {
        command.args(["-c", FIXTURE]);
    } else {
        command.args(["-m", "plangraph.spawner.cli"]);
    }
    let state = home.join("state");
    let output = command
        .args(argv.map(|arg| arg.replace("%S", home.join("state").to_str().unwrap())))
        .current_dir(home)
        .env_clear()
        .env("HOME", home)
        .env("PATH", path)
        .env("INVOCATION_ID", "00000000000000000000000000000001")
        .env("XDG_STATE_HOME", state)
        .env("PYTHONPATH", root.join("src"))
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .unwrap();
    let reply = serde_json::from_slice(&output.stdout)
        .unwrap_or_else(|err| panic!("{err}: {}", String::from_utf8_lossy(&output.stderr)));
    (output.status.code(), reply)
}

#[test]
fn scheduled_probe_discovers_user_harnesses_with_manager_path() {
    let home = tempfile::tempdir().unwrap();
    let local = home.path().join(".local/bin");
    let node = home.path().join(".nvm/versions/node/v24.0.0/bin");
    fs::create_dir_all(&local).unwrap();
    fs::create_dir_all(&node).unwrap();
    for (name, dir) in [
        ("codex", &node),
        ("opencode", &node),
        ("claude", &local),
        ("agy", &local),
    ] {
        symlink("/bin/true", dir.join(name)).unwrap();
    }
    let extra = home.path().join("manager-extra");
    fs::create_dir(&extra).unwrap();
    for path in [
        MANAGER_PATH.to_owned(),
        format!("{}:{MANAGER_PATH}", extra.display()),
    ] {
        let (code, reply) = probe(home.path(), &path, true);
        assert_eq!(code, Some(1), "PATH {path}: {reply}");
        let rows = reply["probes"].as_array().unwrap();
        assert_eq!(rows.len(), 4, "{reply}");
        assert!(
            rows.iter()
                .all(|row| row["reason"] == "exit 17" && row["infra"] == false),
            "{reply}"
        );
    }
}

#[test]
fn scheduled_probe_without_harnesses_reports_infrastructure() {
    let home = tempfile::tempdir().unwrap();
    let empty = home.path().join("empty");
    fs::create_dir(&empty).unwrap();
    let (code, reply) = probe(home.path(), empty.to_str().unwrap(), false);
    assert_eq!(code, Some(3), "{reply}");
    let rows = reply["probes"].as_array().unwrap();
    assert!(!rows.is_empty(), "{reply}");
    assert!(
        rows.iter()
            .all(|row| row["state"] == "host-not-ready" && row["infra"] == true),
        "{reply}"
    );
    assert!(!home
        .path()
        .join("state/plangraph/leg-state.sqlite")
        .exists());
}
