//! Target-host fix-round acknowledgement and detached native resume.
use pg_worker_core::run::record::run_dir;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    io::{BufRead, BufReader, Read, Seek, Write},
    os::unix::process::CommandExt,
    path::Path,
    process::{Command, Stdio},
};

const MAX_PROMPT: usize = 1024 * 1024;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    host: String,
    session_id: String,
    cwd: String,
    final_head: String,
    resume_number: u64,
    pub(crate) prompt: String,
}

impl Request {
    pub(crate) fn read(input: impl Read) -> Result<Self, String> {
        // A single decoded prompt byte may need six bytes of JSON escaping.
        const MAX_BODY: u64 = (6 * MAX_PROMPT + 32 * 1024) as u64;
        let mut body = Vec::new();
        input
            .take(MAX_BODY + 1)
            .read_to_end(&mut body)
            .map_err(|_| "cannot read native resume request")?;
        if body.len() as u64 > MAX_BODY {
            return Err("native resume request exceeds the size limit".into());
        }
        let request: Self = serde_json::from_slice(&body).map_err(|_| {
            "native resume requires host, session, worktree, head, round and prompt"
        })?;
        if request.prompt.trim().is_empty()
            || request.prompt.len() > MAX_PROMPT
            || request.prompt.contains('\0')
        {
            return Err("resume prompt must contain between 1 byte and 1 MiB of text".into());
        }
        Ok(request)
    }

    fn digest(&self) -> String {
        format!("{:x}", Sha256::digest(self.prompt.as_bytes()))
    }

    pub(crate) fn check(&self, id: &str, record: &Value) -> Result<Option<Value>, String> {
        if record["run_id"] != id
            || record["run_kind"] != "native-codex"
            || record["role"] != "implement"
            || record["overflow_remote"] == true
            || record["origin_receipt"] == true
        {
            return Err("resume must run on the native writer's owning host".into());
        }
        if self.host.is_empty() || record["host"] != self.host {
            return Err("resume host differs from the admitted writer".into());
        }
        if self.session_id.is_empty() || record["session_id"] != self.session_id {
            return Err("resume session differs from the admitted writer".into());
        }
        if !Path::new(&self.cwd).is_absolute() || record["cwd"] != self.cwd {
            return Err("resume worktree differs from the admitted writer".into());
        }
        let round = match record.get("resume_number") {
            None => 0,
            Some(value) => value.as_u64().ok_or("invalid writer resume round")?,
        };
        if self.resume_number.checked_add(1) == Some(round)
            && record["resume_from"]["head"] == self.final_head
            && record["resume_prompt_sha256"] == self.digest()
        {
            return Ok(Some(acknowledged(record)));
        }
        if self.final_head.is_empty() || record["final_head"] != self.final_head {
            return Err("resume head differs from the ended writer".into());
        }
        if self.resume_number != round {
            return Err("resume round differs from the ended writer".into());
        }
        Ok(None)
    }

    pub(crate) fn remember(&self, record: &mut Value) {
        record["resume_prompt_sha256"] = json!(self.digest());
    }
}

pub(crate) fn acknowledged(record: &Value) -> Value {
    json!({"ok":true,"run_id":record["run_id"],"resume_number":record["resume_number"],
           "started":record["started"]})
}

pub(crate) fn refused(id: &str, reason: String) -> Value {
    json!({"ok":false,"run_id":id,"error":reason})
}

pub(crate) fn launch(args: &[String], input: impl Read, out: &mut dyn Write) -> Result<(), String> {
    let usage = "usage: pg-spawnd resume-local RUN_ID [--state PATH]";
    let (id, state) = match args {
        [id] => (id, None),
        [id, flag, state] if flag == "--state" => (id, Some(state.as_str())),
        _ => return Err(usage.into()),
    };
    let start = || -> Result<Value, String> {
        let state = crate::state::resolve_state(state, |key| std::env::var_os(key))?;
        let record_path = run_dir(&state, id)
            .map_err(|_| "unknown run id")?
            .join("record.json");
        let record = crate::records::read_record_file(&record_path)
            .map_err(|_| "native writer record unavailable")?;
        let request = Request::read(input)?;
        if let Some(receipt) = request.check(id, &record)? {
            return Ok(receipt);
        }

        let mut body = tempfile::tempfile().map_err(|_| "cannot create private resume input")?;
        serde_json::to_writer(&mut body, &request)
            .map_err(|_| "cannot write private resume input")?;
        body.rewind()
            .map_err(|_| "cannot rewind private resume input")?;
        let mut cmd =
            Command::new(std::env::current_exe().map_err(|_| "cannot resolve pg-spawnd")?);
        cmd.args(["resume", id, "--request-stdin", "--state"])
            .arg(state)
            .stdin(Stdio::from(body))
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        // The writer outlives the SSH or WSL hop that delivers this acknowledgement.
        // SAFETY: setsid is async-signal-safe and only changes the forked child.
        unsafe {
            cmd.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = cmd
            .spawn()
            .map_err(|_| "cannot start native resume supervisor")?;
        let mut line = String::new();
        BufReader::new(
            child
                .stdout
                .take()
                .ok_or("native resume acknowledgement pipe missing")?,
        )
        .take(16 * 1024)
        .read_line(&mut line)
        .map_err(|_| "cannot read native resume acknowledgement")?;
        let response: Value = serde_json::from_str(&line)
            .map_err(|_| "native resume supervisor did not acknowledge; retry the same request")?;
        if response["ok"] == false {
            let _ = child.wait();
        }
        Ok(response)
    };
    let response = start().unwrap_or_else(|error| refused(id, error));
    writeln!(out, "{response}").map_err(|e| e.to_string())
}
