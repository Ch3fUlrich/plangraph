//! Token-free client of existing local/SSH pg-spawnd shims.
use crate::{
    host_command, host_link, host_overflow::Launch, hosts::HostRegistry,
    package_placement::Candidate, run::record::valid_run_id,
};
use serde_json::{json, Value};
use std::{
    io::{self, BufRead, BufReader, Read, Write},
    path::Path,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, Instant},
};

const TIMEOUT: Duration = Duration::from_secs(30);

pub fn classify(reply: &Value, host: &str) -> Launch {
    if reply["error"]["data"]["type"] == "SpawnRefused" {
        return Launch::Refused(
            reply["error"]["message"]
                .as_str()
                .unwrap_or("admission refused")
                .into(),
        );
    }
    let receipt = reply["result"]["content"].as_array().and_then(|rows| {
        rows.iter().find_map(|r| {
            serde_json::from_str::<Value>(r["text"].as_str()?)
                .ok()
                .filter(Value::is_object)
        })
    });
    let Some(receipt) = receipt else {
        return Launch::Uncertain("missing launch receipt".into());
    };
    if receipt["host"].as_str() != Some(host) {
        return Launch::Uncertain("launch receipt host mismatch".into());
    }
    if receipt["status"] == "refused" {
        return Launch::Refused(
            receipt["error"]
                .as_str()
                .unwrap_or("admission refused")
                .into(),
        );
    }
    if !receipt["run_id"].as_str().is_some_and(valid_run_id)
        || receipt["status"]
            .as_str()
            .and_then(crate::run::record::Status::parse)
            .is_none()
    {
        return Launch::Uncertain("invalid launch receipt".into());
    }
    Launch::Started(receipt)
}
pub fn launch(registry: &HostRegistry, host: &Candidate, request: &Value) -> Launch {
    launch_inner(registry, host, request, None)
}

/// Keep the selected host when the link monitor has recorded an outage.
pub fn launch_with_state(
    registry: &HostRegistry,
    host: &Candidate,
    request: &Value,
    state: &Path,
) -> Launch {
    launch_inner(registry, host, request, Some(state))
}

fn launch_inner(
    registry: &HostRegistry,
    host: &Candidate,
    request: &Value,
    state: Option<&Path>,
) -> Launch {
    let mut cmd = if registry
        .resolve(&registry.self_host)
        .is_some_and(|local| local.id == host.id)
    {
        Command::new("pg-spawnd")
    } else {
        let Some(dest) = host.ssh.as_deref() else {
            return Launch::Refused("no SSH route".into());
        };
        let mut cmd = match host_command::ssh(dest) {
            Ok(cmd) => cmd,
            Err(_) => return Launch::Refused("invalid SSH route".into()),
        };
        if let Some(state) = state {
            match host_link::ssh_down_since(state, dest) {
                Ok(Some(since)) => {
                    return Launch::Waiting(format!("{} unreachable since {since}", host.id));
                }
                Ok(None) => (),
                Err(error) => return Launch::Uncertain(format!("host link state: {error}")),
            }
        }
        if let Some(distro) = registry
            .resolve(&host.id)
            .and_then(|host| host.wsl_distro.as_deref())
        {
            if !host_command::valid_wsl_distro(distro) {
                return Launch::Refused("invalid WSL distribution".into());
            }
            cmd.args(["wsl.exe", "--distribution", distro, "--exec"]);
        }
        cmd.arg("pg-spawnd");
        cmd
    };
    cmd.arg("shim");
    launch_using(&mut cmd, &host.id, request)
}
pub fn launch_using(cmd: &mut Command, host: &str, request: &Value) -> Launch {
    host_command::isolate_git(cmd);
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return Launch::Refused("host shim could not start".into()),
    };
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout.take(1024 * 1024)).lines() {
            let value =
                line.and_then(|l| serde_json::from_str::<Value>(&l).map_err(io::Error::other));
            if tx.send(value).is_err() {
                break;
            }
        }
    });
    let (reply_tx, reply_rx) = mpsc::sync_channel(1);
    let request = request.clone();
    std::thread::spawn(move || {
        let start = Instant::now();
        let result = (|| -> io::Result<Value> {
            let mut send = |value: Value| -> io::Result<()> {
                writeln!(stdin, "{value}")?;
                stdin.flush()
            };
            let receive = |id: u64| -> io::Result<Value> {
                loop {
                    let value = rx
                        .recv_timeout(TIMEOUT.saturating_sub(start.elapsed()))
                        .map_err(|_| io::Error::other("shim timeout"))??;
                    if value["id"].as_u64() == Some(id) {
                        return Ok(value);
                    }
                }
            };
            send(
                json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"pg-host-overflow","version":"0.1.0"}}}),
            )?;
            if receive(1)?.get("error").is_some() {
                return Err(io::Error::other("initialize failed"));
            }
            send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))?;
            send(
                json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"plangraph_agent_spawn","arguments":request}}),
            )?;
            receive(2)
        })();
        let _ = reply_tx.send(result);
    });
    let result = reply_rx.recv_timeout(TIMEOUT);
    #[cfg(unix)]
    // SAFETY: this process owns the isolated client process group, not the daemon.
    unsafe {
        libc::killpg(child.id() as i32, libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
    match result {
        Ok(Ok(reply)) => classify(&reply, host),
        _ => Launch::Uncertain("host connection or launch receipt unavailable".into()),
    }
}
