//! Recoverable publication of a previously verified quarantine head.
use serde::{Deserialize, Serialize};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Debug, thiserror::Error)]
pub enum PublishError {
    #[error("invalid_publish_input")]
    Invalid,
    #[error("git_publish_failed")]
    Git,
    #[error("invalid_step_log")]
    Log,
    #[error("invalid_step_log_line_{0}")]
    LogLine(usize),
    #[error("io")]
    Io(#[from] std::io::Error),
}
pub type Result<T> = std::result::Result<T, PublishError>;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Outcome {
    Published,
    AlreadyPublished,
    #[serde(rename = "finalize_conflict")]
    Conflict,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PrRef {
    pub number: u64,
}
/// Implementations must find by head branch before creating or updating.
pub trait PrStep {
    fn find_or_update(&self, lane: &str, head_sha: &str, title: &str, body: &str) -> Result<PrRef>;
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Step {
    LaneCas {
        head: String,
        expected_old: Option<String>,
        outcome: Outcome,
    },
    Pr {
        number: u64,
    },
    Done,
}
pub trait StepLog {
    fn load(&self, run_id: &str) -> Result<Vec<Step>>;
    fn append(&self, run_id: &str, step: Step) -> Result<()>;
}
pub struct LocalFileStepLog {
    pub dir: PathBuf,
}
fn safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}
impl LocalFileStepLog {
    fn path(&self, run_id: &str) -> Result<PathBuf> {
        if !safe_id(run_id) {
            return Err(PublishError::Invalid);
        }
        Ok(self.dir.join(format!("{run_id}.jsonl")))
    }
}
impl StepLog for LocalFileStepLog {
    fn load(&self, run_id: &str) -> Result<Vec<Step>> {
        let bytes = match fs::read(self.path(run_id)?) {
            Ok(bytes) => bytes,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            Err(e) => return Err(e.into()),
        };
        bytes
            .split_inclusive(|b| *b == b'\n')
            .enumerate()
            .filter(|(_, line)| line.ends_with(b"\n"))
            .map(|(i, line)| serde_json::from_slice(line).map_err(|_| PublishError::LogLine(i + 1)))
            .collect()
    }
    fn append(&self, run_id: &str, step: Step) -> Result<()> {
        let path = self.path(run_id)?;
        fs::create_dir_all(&self.dir)?;
        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(path)?;
        // Remove an incomplete record before adding the next durable record.
        use std::io::{Read, Seek, SeekFrom};
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        let end = bytes.iter().rposition(|b| *b == b'\n').map_or(0, |i| i + 1);
        file.set_len(end as u64)?;
        file.seek(SeekFrom::End(0))?;
        let mut record = serde_json::to_vec(&step).map_err(|_| PublishError::Log)?;
        record.push(b'\n');
        file.write_all(&record)?;
        file.sync_all()?;
        fs::File::open(&self.dir)?.sync_all()?;
        Ok(())
    }
}
/// Calls for one run must be serialized by the caller's finalizer lease.
pub struct Remote<'a> {
    pub root: &'a Path,
    pub name: &'a str,
}
fn git(remote: &Remote<'_>, args: &[&str]) -> Result<std::process::Output> {
    git_with_env(remote, args, &[])
}
fn git_with_env(
    remote: &Remote<'_>,
    args: &[&str],
    env: &[(&str, &str)],
) -> Result<std::process::Output> {
    if !remote.root.is_absolute() || !valid_remote(remote.name) {
        return Err(PublishError::Invalid);
    }
    let mut command = crate::workspace_resolver::git_command(remote.root);
    command.envs(env.iter().copied());
    for key in std::env::vars_os()
        .map(|(key, _)| key)
        .chain(env.iter().map(|(key, _)| (*key).into()))
    {
        let name = key.to_string_lossy();
        if name.starts_with("GIT_CONFIG_KEY_") || name.starts_with("GIT_CONFIG_VALUE_") {
            command.env_remove(key);
        }
    }
    for key in [
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_INDEX_FILE",
    ] {
        command.env_remove(key);
    }
    Ok(command
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_SYSTEM", "/dev/null")
        .env("GIT_ALLOW_PROTOCOL", "file:https:ssh")
        .args(["--no-replace-objects", "-c", "core.hooksPath="])
        .args(args)
        .output()?)
}
fn valid_remote(name: &str) -> bool {
    if name.is_empty()
        || name.contains("::")
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"/._-:@%+~".contains(&b))
    {
        return false;
    }
    if name.starts_with('/') {
        return true;
    }
    if let Some(rest) = name
        .strip_prefix("https://")
        .or_else(|| name.strip_prefix("ssh://"))
    {
        return rest.split_once('/').is_some_and(|(host, path)| {
            !host.is_empty() && !host.starts_with('-') && !path.is_empty()
        });
    }
    name.strip_prefix("git@")
        .and_then(|rest| rest.split_once(':'))
        .is_some_and(|(host, path)| {
            !host.is_empty() && !host.starts_with('-') && !host.contains('/') && !path.is_empty()
        })
}
fn validate(head: &str, lane: &str, old: Option<&str>, remote: &Remote<'_>) -> Result<()> {
    if !crate::writer_result::oid(head) || old.is_some_and(|s| !crate::writer_result::oid(s)) {
        return Err(PublishError::Invalid);
    }
    if !git(remote, &["check-ref-format", &format!("refs/heads/{lane}")])?
        .status
        .success()
    {
        return Err(PublishError::Invalid);
    }
    Ok(())
}
fn lane_head(remote: &Remote<'_>, reference: &str) -> Result<Option<String>> {
    let out = git(
        remote,
        &["ls-remote", "--refs", "--", remote.name, reference],
    )?;
    if !out.status.success() {
        return Err(PublishError::Git);
    }
    let text = std::str::from_utf8(&out.stdout).map_err(|_| PublishError::Git)?;
    Ok(text.lines().find_map(|line| {
        line.split_once('\t')
            .filter(|(_, name)| *name == reference)
            .map(|(sha, _)| sha.to_owned())
    }))
}
pub fn publish(
    head_sha: &str,
    lane: &str,
    expected_old: Option<&str>,
    remote: &Remote<'_>,
) -> Result<Outcome> {
    validate(head_sha, lane, expected_old, remote)?;
    let reference = format!("refs/heads/{lane}");
    let current = lane_head(remote, &reference)?;
    if current.as_deref() == Some(head_sha) {
        return Ok(Outcome::AlreadyPublished);
    }
    if current.as_deref() != expected_old {
        return Ok(Outcome::Conflict);
    }
    let lease = format!(
        "--force-with-lease={reference}:{}",
        expected_old.unwrap_or("")
    );
    let target = format!("{head_sha}:{reference}");
    let out = git(
        remote,
        &["push", "--porcelain", remote.name, &target, &lease],
    )?;
    if out.status.success() {
        return Ok(Outcome::Published);
    }
    let current = lane_head(remote, &reference)?;
    if current.as_deref() == Some(head_sha) {
        return Ok(Outcome::AlreadyPublished);
    }
    if current.as_deref() != expected_old {
        return Ok(Outcome::Conflict);
    }
    Err(PublishError::Git)
}
pub fn finalize_publish(
    run_id: &str,
    head_sha: &str,
    lane: &str,
    expected_old: Option<&str>,
    remote: &Remote<'_>,
    pr: &dyn PrStep,
    log: &dyn StepLog,
) -> Result<Outcome> {
    if !safe_id(run_id) {
        return Err(PublishError::Invalid);
    }
    validate(head_sha, lane, expected_old, remote)?;
    let steps = log.load(run_id)?;
    let mut outcome = None;
    let mut has_pr = false;
    let mut done = false;
    for step in steps {
        match step {
            Step::LaneCas {
                ref head,
                expected_old: ref old,
                ..
            } if head != head_sha || old.as_deref() != expected_old => {
                return Err(PublishError::Invalid)
            }
            Step::LaneCas {
                head,
                expected_old: old,
                outcome: result,
            } if outcome.is_none() && head == head_sha && old.as_deref() == expected_old => {
                outcome = Some(result)
            }
            Step::Pr { .. }
                if matches!(
                    outcome,
                    Some(Outcome::Published | Outcome::AlreadyPublished)
                ) && !has_pr
                    && !done =>
            {
                has_pr = true
            }
            Step::Done if has_pr && !done => done = true,
            _ => return Err(PublishError::Log),
        }
    }
    let outcome = match outcome {
        Some(outcome) => outcome,
        None => {
            let outcome = publish(head_sha, lane, expected_old, remote)?;
            log.append(
                run_id,
                Step::LaneCas {
                    head: head_sha.into(),
                    expected_old: expected_old.map(str::to_owned),
                    outcome,
                },
            )?;
            outcome
        }
    };
    if outcome == Outcome::Conflict || done {
        return Ok(outcome);
    }
    if !has_pr {
        let reference =
            pr.find_or_update(lane, head_sha, lane, &format!("Verified head: {head_sha}"))?;
        log.append(
            run_id,
            Step::Pr {
                number: reference.number,
            },
        )?;
    }
    log.append(run_id, Step::Done)?;
    Ok(outcome)
}
#[cfg(test)]
#[path = "finalize_publish_tests.rs"]
mod tests;
