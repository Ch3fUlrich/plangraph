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

/// The stop command for a Claude job: `claude stop` takes the 8-char job id.
pub fn claude_stop(rec: &Record) -> Option<Vec<String>> {
    let job = rec.job_id()?;
    Some(vec!["claude".into(), "stop".into(), job])
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
