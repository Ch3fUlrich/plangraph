//! Private, ordered per-run steer messages. The writer records delivery after a turn.
#[cfg(test)]
use crate::test_python;

use crate::{errors::ToolError, placement::Args};
use pg_worker_core::{
    lockfile,
    run::{record::run_dir, record_io::atomic_write_private},
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::Path,
    time::Duration,
};

const MAX_MESSAGE: usize = 16 * 1024;
const MAX_MESSAGES: u64 = 128;

#[derive(Deserialize, Serialize)]
pub(crate) struct Message {
    pub id: u64,
    pub message: String,
    queued_at: String,
}

fn refusal(id: &str, error: &str) -> Value {
    json!({"run_id":id,"ok":false,"steered":false,"error":error})
}

fn validate(message: &str) -> Result<(), &'static str> {
    if message.trim().is_empty() || message.len() > MAX_MESSAGE || message.contains('\0') {
        return Err("steer message must contain between 1 byte and 16 KiB of text");
    }
    Ok(())
}

fn read_queue(directory: &Path) -> Result<Vec<Message>, String> {
    let mut file = match crate::records::open_record(&directory.join("steer.json")) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(format!("steer queue unreadable: {error}")),
    };
    // JSON can escape a single message byte into six bytes.
    let limit = MAX_MESSAGES * (6 * MAX_MESSAGE as u64 + 256);
    let mut bytes = Vec::new();
    (&mut file)
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > limit {
        return Err("steer queue too large".into());
    }
    serde_json::from_slice(&bytes).map_err(|_| "invalid steer queue".into())
}

fn append(state: &Path, id: &str, message: &str) -> Result<Value, String> {
    validate(message)?;
    let directory = run_dir(state, id).map_err(|_| "unknown run id")?;
    if !directory.join("record.json").is_file() {
        return Err("unknown run id".into());
    }
    let _lock = lockfile::try_lock(&directory.join("steer.lock"))
        .map_err(|e| e.to_string())?
        .ok_or("steer queue busy")?;
    let record = crate::records::read_record_file(&directory.join("record.json"))
        .map_err(|e| e.into_error_data().message.into_owned())?;
    if record["run_id"] != id
        || record["run_kind"] != "native-codex"
        || record["overflow_remote"] == true
        || record["origin_receipt"] == true
    {
        return Err("steer requires a native writer on its owning host".into());
    }
    if record["status"] != "running" {
        return Err(if record["status"] == "queued" {
            "native writer is queued"
        } else {
            "native writer is finished"
        }
        .into());
    }
    let mut messages = read_queue(&directory)?;
    if messages.len() as u64 >= MAX_MESSAGES {
        return Err("steer queue full".into());
    }
    let message_id = messages.len() as u64 + 1;
    messages.push(Message {
        id: message_id,
        message: message.into(),
        queued_at: now(),
    });
    let bytes = serde_json::to_vec(&messages).map_err(|e| e.to_string())?;
    atomic_write_private(&directory.join("steer.json"), &bytes).map_err(|e| e.to_string())?;
    Ok(
        json!({"run_id":id,"host":record["host"],"ok":true,"steered":true,"queued":true,"message_id":message_id}),
    )
}

pub(crate) fn pending(directory: &Path, record: &Value) -> Result<Vec<Message>, String> {
    let delivered = record["steer_messages"].as_array();
    Ok(read_queue(directory)?
        .into_iter()
        .filter(|message| {
            delivered.is_none_or(|rows| {
                rows.iter()
                    .all(|row| row["id"].as_u64() != Some(message.id))
            })
        })
        .collect())
}

pub(crate) fn delivered(record: &mut Value, messages: &[Message]) {
    if messages.is_empty() {
        return;
    }
    let entries = record
        .as_object_mut()
        .unwrap()
        .entry("steer_messages")
        .or_insert_with(|| json!([]));
    let delivered_at = now();
    for message in messages {
        let mut entry = json!(message);
        entry["delivered_at"] = json!(delivered_at);
        entries.as_array_mut().unwrap().push(entry);
    }
}

fn now() -> String {
    chrono::Utc::now().to_rfc3339()
}

pub(crate) fn local_cli(
    args: &[String],
    input: impl Read,
    output: &mut dyn Write,
) -> Result<(), String> {
    let (id, state) = match args {
        [id] => (id, None),
        [id, flag, path] if flag == "--state" => (id, Some(path.as_str())),
        _ => return Err("usage: pg-spawnd steer-local RUN_ID [--state PATH]".into()),
    };
    let state = crate::state::resolve_state(state, |key| std::env::var_os(key))?;
    let mut message = String::new();
    let reply = input
        .take(MAX_MESSAGE as u64 + 1)
        .read_to_string(&mut message)
        .map_err(|_| "steer message is unreadable".to_string())
        .and_then(|_| append(&state, id, &message))
        .unwrap_or_else(|reason| refusal(id, &reason));
    writeln!(output, "{reply}").map_err(|e| e.to_string())
}

pub(crate) fn mcp(state: &Path, args: &Args) -> Result<Value, ToolError> {
    let id = args["run_id"]
        .as_str()
        .ok_or_else(|| crate::placement::parameter("run_id must be a string"))?;
    let message = args
        .get("message")
        .and_then(Value::as_str)
        .ok_or_else(|| crate::placement::parameter("message must be a string"))?;
    let directory = match run_dir(state, id) {
        Ok(directory) => directory,
        Err(_) => return Ok(refusal(id, "unknown run id")),
    };
    if let Err(error) = validate(message) {
        return Ok(refusal(id, error));
    }
    let host = args
        .get("host")
        .map(|v| {
            v.as_str()
                .ok_or_else(|| crate::placement::parameter("host must be a string"))
        })
        .transpose()?;
    let path = directory.join("record.json");
    if host.is_none() && path.exists() {
        let record = crate::records::read_record_file(&path)?;
        if record["overflow_remote"] != true && record["origin_receipt"] != true {
            return Ok(append(state, id, message).unwrap_or_else(|e| refusal(id, &e)));
        }
    }
    let mut argv = vec!["steer".into(), id.into()];
    if let Some(host) = host {
        argv.extend(["--host".into(), host.into()]);
    }
    let (_, stdout, _) =
        crate::adapter::Adapter(state).run_input(&argv, Duration::from_secs(30), Some(message))?;
    let reply: Value = serde_json::from_slice(&stdout)
        .map_err(|_| ToolError::Internal("steer host route returned no receipt".into()))?;
    if reply["run_id"] != id || !reply["ok"].is_boolean() {
        return Err(ToolError::Internal(
            "steer host route returned an invalid receipt".into(),
        ));
    }
    Ok(reply)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_steer_refuses_unknown_target_with_a_receipt() {
        let state = tempfile::tempdir().unwrap();
        let args = ["run-unknown", "--state", state.path().to_str().unwrap()].map(String::from);
        let mut output = Vec::new();
        local_cli(&args, "fixture message".as_bytes(), &mut output).unwrap();
        let receipt: Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(receipt, refusal("run-unknown", "unknown run id"));
    }

    #[cfg(unix)]
    #[test]
    fn native_steer_cli_and_mcp_use_configured_posix_and_wsl_routes() {
        let state = tempfile::tempdir().unwrap();
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
        let mut command = test_python::command();
        let output = command
            .env("PYTHONPATH", source)
            .arg("-c")
            .arg(
                r#"
import asyncio, contextlib, io, json, re, sys
from types import SimpleNamespace
from unittest.mock import patch
from plangraph.spawner import cli, mcp, remote_ps

state = sys.argv[1]
message = 'scope message with quotes and newline\nsecond line'
run_id = 'run-fixture'
host = dict(repo=state, ssh='fixture', spawner_src=state, state_dir=state)
routes = {'hosts': {'posix': host, 'wsl': dict(host, wsl_distro='Fixture')}}
calls = []
def ssh(argv, *, input, **kwargs):
    assert argv[0] == 'ssh' and argv[-2] == 'fixture', argv
    if argv[-1].startswith('wsl.exe'):
        step, = json.loads(input)['steps']
        assert step['stdin_text'] == message and step['argv'] == [
            'pg-spawnd', 'steer-local', run_id, '--state', state], step
        begin, end = step['receipt']['begin'], step['receipt']['end']
        calls.append('wsl')
    else:
        assert input.decode() == message and message not in argv[-1], argv
        assert f'pg-spawnd steer-local {run_id} --state' in argv[-1], argv
        begin, end = re.findall(r'PLANGRAPH_RECEIPT_[a-f0-9]+_(?:BEGIN|END)', argv[-1])
        calls.append('posix')
    receipt = json.dumps(dict(run_id=run_id, ok=True, steered=True, queued=True))
    return SimpleNamespace(stdout=f'{begin}\n{receipt}\n{end}\n'.encode())

with patch('plangraph.spawner.overflow.registry_caps', return_value={}), \
     patch('plangraph.spawner.overflow.placement_config', return_value=routes), \
     patch.object(remote_ps.subprocess, 'run', side_effect=ssh):
    stdout = io.StringIO()
    with patch('sys.stdin', io.StringIO(message)), contextlib.redirect_stdout(stdout):
        assert cli.main(['--state', state, 'steer', run_id, '--host', 'posix']) == 0
    assert json.loads(stdout.getvalue())['queued']
    server = mcp.MCPSpawnerServer(state=state, source_identity=lambda: 'fixture')
    reply = asyncio.run(server.call_tool('plangraph_agent_steer',
                       dict(run_id=run_id, message=message, host='wsl')))
    assert reply['queued'] and calls == ['posix', 'wsl'], (reply, calls)
"#,
            )
            .arg(state.path())
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
