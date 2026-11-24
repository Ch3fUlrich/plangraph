//! The per-agent stop command line. Argument vectors only; no shell.

use crate::record::Record;

/// The stop command for a Claude job: `claude stop` takes the 8-char job id.
pub fn claude_stop(rec: &Record) -> Option<Vec<String>> {
    let job = rec.job_id()?;
    Some(vec!["claude".into(), "stop".into(), job])
}
