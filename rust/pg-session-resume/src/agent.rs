//! The per-agent stop and resume command lines. Argument vectors only; no shell.

use crate::record::{Agent, Record};

pub struct Cmd {
    pub argv: Vec<String>,
    /// True when the command runs for the whole fix round and must not be waited for.
    pub detached: bool,
}

fn own(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|s| (*s).to_owned()).collect()
}

/// The stop command, None when the record names nothing that can be stopped.
pub fn stop_cmd(rec: &Record) -> Option<Vec<String>> {
    match rec.agent() {
        Agent::Claude => {
            // `claude stop` takes the 8-char job id, never a longer id.
            let job = rec
                .str("job_id")
                .or_else(|| rec.handle().map(|h| &h[..h.len().min(8)]))?;
            Some(vec!["claude".into(), "stop".into(), job.to_owned()])
        }
        Agent::Codex | Agent::OpenCode => {
            let pid = rec.u64("pid").filter(|p| *p > 1)?;
            Some(vec!["kill".into(), pid.to_string()])
        }
    }
}

/// The resume command carrying exactly `prompt`.
pub fn resume_cmd(agent: Agent, handle: &str, prompt: &str) -> Cmd {
    let mut argv = match agent {
        Agent::Claude => own(&["claude", "--bg", "--resume", handle]),
        Agent::Codex => own(&["codex", "exec", "resume", handle]),
        Agent::OpenCode => own(&["opencode", "run", "--session", handle]),
    };
    argv.push(prompt.to_owned());
    Cmd {
        argv,
        detached: agent != Agent::Claude,
    }
}
