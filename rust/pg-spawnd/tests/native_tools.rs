//! Tool admission runs on the target environment before any run is recorded.
#![cfg(unix)]
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::{fs, os::unix::fs::PermissionsExt, process::Command};

#[test]
fn native_spawn_refuses_each_missing_tool_before_record_creation() {
    let dir = std::env::temp_dir().join(format!("native-tools-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let python = test_python::command()
        .args(["-c", "import sys; print(sys.executable)"])
        .output()
        .unwrap();
    let python = String::from_utf8(python.stdout).unwrap();
    for missing in ["cargo", "git", "codex"] {
        let bin = dir.join(missing);
        fs::create_dir_all(&bin).unwrap();
        for tool in ["cargo", "git", "codex"] {
            if tool != missing {
                let path = bin.join(tool);
                pg_worker_core::executable_test_support::write_executable(
                    &path,
                    "#!/bin/sh\nexit 0\n",
                );
            }
        }
        let out = Command::new(python.trim())
            .current_dir(&root)
            .env("PYTHONPATH", root.join("src"))
            .env("PATH", &bin)
            .env("NATIVE_BINARY", env!("CARGO_BIN_EXE_pg-spawnd"))
            .env("MISSING", missing)
            .args(["-c", r#"
import asyncio, os
from unittest.mock import patch
from pathlib import Path
from plangraph.spawner.service import SpawnService
from plangraph.spawner.overflow import native_spawn
from plangraph.planner.routing import SpawnRefused
async def check():
    service = SpawnService(state=Path(os.environ['PATH']).parent / ('state-' + os.environ['MISSING']))
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']), patch.object(service, '_save', wraps=service._save) as save, patch.object(service, '_path', wraps=service._path) as path:
        try:
            await native_spawn(service, dict(repo=os.getcwd()), dict(prompt='unused'))
        except SpawnRefused as exc:
            assert 'tool_missing: ' + os.environ['MISSING'] in str(exc), str(exc)
        else:
            raise AssertionError('missing tool was admitted')
        save.assert_not_called()
        path.assert_not_called()
asyncio.run(check())
"#])
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn native_tools_record_absolute_paths_and_refuse_nonexecutables() {
    let dir = tempfile::tempdir().unwrap();
    let bin = dir.path().join("bin");
    fs::create_dir(&bin).unwrap();
    for tool in ["cargo", "git", "codex"] {
        let path = bin.join(tool);
        pg_worker_core::executable_test_support::write_executable(&path, "#!/bin/sh\nexit 0\n");
    }
    let probe = || {
        Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .arg("native-tools")
            .current_dir(dir.path())
            .env("PATH", "bin")
            .output()
            .unwrap()
    };
    let out = probe();
    assert!(out.status.success(), "{:?}", out);
    let record: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(record["tool_path"], bin.to_str().unwrap());
    for tool in ["cargo", "git", "codex"] {
        assert_eq!(
            record["resolved_tools"][tool],
            bin.join(tool).to_str().unwrap()
        );
    }
    fs::set_permissions(bin.join("cargo"), fs::Permissions::from_mode(0o644)).unwrap();
    let out = probe();
    assert!(!out.status.success());
    assert_eq!(
        String::from_utf8(out.stderr).unwrap().trim(),
        "tool_missing: cargo"
    );
}

#[test]
fn native_spawn_refuses_a_failed_sandbox_probe_before_worktree_creation() {
    let tmp = tempfile::tempdir().unwrap();
    let bin = tmp.path().join("bin");
    fs::create_dir(&bin).unwrap();
    for tool in ["cargo", "git", "codex"] {
        let path = bin.join(tool);
        let body = if tool == "codex" {
            "#!/bin/sh\n[ \"$1\" = sandbox ] || exit 7\n>&2 echo 'bwrap: setting up uid map: Permission denied'\nexit 1\n"
        } else {
            "#!/bin/sh\nexit 0\n"
        };
        pg_worker_core::executable_test_support::write_executable(&path, body);
    }
    let out = test_python::command()
        .current_dir(tmp.path())
        .env("HOME", tmp.path())
        .env("PATH", &bin)
        .env("NATIVE_BINARY", env!("CARGO_BIN_EXE_pg-spawnd"))
        .args(["-c", r#"
import asyncio, os
from pathlib import Path
from unittest.mock import patch
from plangraph.spawner.overflow import native_spawn
from plangraph.spawner.service import SpawnService

async def check():
    service = SpawnService(state=Path.cwd() / 'state')
    card = dict(repo=os.getcwd(), run_kind='native-codex')
    with patch('plangraph.fleet_snapshot._projection_binary', return_value=os.environ['NATIVE_BINARY']):
        denied = await native_spawn(service, card, dict(prompt='unused'))
    assert denied['status'] == 'refused', denied
    assert denied['reason'].startswith('sandbox_unavailable: bwrap: setting up uid map: Permission denied'), denied
    assert 'AppArmor' in denied['reason'] and denied['exit_code'] == 2, denied
    assert service.result(denied['run_id'])['reason'] == denied['reason']
    assert not service.tasks and not list(service.state.rglob('native-request.json'))
asyncio.run(check())
"#])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
