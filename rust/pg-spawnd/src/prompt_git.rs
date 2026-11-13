//! Bounded, registry-rooted diff reads. Never consult adapter cwd.
use crate::prompt::InlineDiffError;
use pg_worker_core::{hosts::RepoEntry, workspace::DiffRef, workspace_resolver};
use std::{
    io::Read,
    path::Path,
    process::Stdio,
    sync::mpsc,
    time::{Duration, Instant},
};

pub(crate) fn read_diff(
    repo: &RepoEntry,
    diff: &DiffRef,
    cap: usize,
) -> Result<Vec<u8>, InlineDiffError> {
    validate(diff)?;
    for sha in [&diff.base_sha, &diff.head_sha] {
        workspace_resolver::ensure_commit(repo, sha, Duration::from_secs(10))
            .map_err(|_| InlineDiffError::Git)?;
    }
    let mut cmd = workspace_resolver::git_command(Path::new(&repo.root));
    cmd.args([
        "-c",
        "core.hooksPath=",
        "-c",
        "core.fsmonitor=false",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        &diff.base_sha,
        &diff.head_sha,
        "--",
    ])
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    collect_diff(
        cmd.spawn().map_err(|_| InlineDiffError::Git)?,
        cap,
        Duration::from_secs(10),
    )
}

fn collect_diff(
    mut child: std::process::Child,
    cap: usize,
    timeout: Duration,
) -> Result<Vec<u8>, InlineDiffError> {
    let deadline = Instant::now() + timeout;
    let mut stdout = child.stdout.take().ok_or(InlineDiffError::Git)?;
    let (tx, rx) = mpsc::sync_channel(1);
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .by_ref()
            .take(cap.saturating_add(1) as u64)
            .read_to_end(&mut bytes);
        // Git for Windows may be a wrapper. Drain without retaining more bytes
        // so its actual Git child can exit before the checkout is removed.
        let result = result.and_then(|_| std::io::copy(&mut stdout, &mut std::io::sink()));
        drop(stdout);
        let _ = tx.send(result.map(|_| bytes));
    });
    let result = rx.recv_timeout(deadline.saturating_duration_since(Instant::now()));
    let mut exited = child.try_wait().map_err(|_| InlineDiffError::Git)?;
    while result.is_ok() && (exited.is_none() || !reader.is_finished()) {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        std::thread::sleep(remaining.min(Duration::from_millis(5)));
        exited = child.try_wait().map_err(|_| InlineDiffError::Git)?;
    }
    let completed =
        result.is_ok() && exited.is_some() && reader.is_finished() && Instant::now() < deadline;
    let status = reap_after_kill(&mut child, |child| {
        // This group was created by this invocation; never signal a registry process.
        if exited.is_none() && child.try_wait()?.is_none() {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            return child.kill();
        }
        Ok(())
    })
    .map_err(|_| InlineDiffError::Git)?;
    // A descendant outside our owned group may still hold stdout. Never wait
    // for its EOF on failure; the detached reader exits when that holder closes.
    if !completed {
        return Err(InlineDiffError::Git);
    }
    // is_finished above makes this a nonblocking join, within the same budget
    // used for pipe draining and child exit, not an additional grace period.
    reader.join().map_err(|_| InlineDiffError::Git)?;
    let bytes = result
        .map_err(|_| InlineDiffError::Git)?
        .map_err(|_| InlineDiffError::Git)?;
    if bytes.len() > cap {
        return Err(InlineDiffError::TooLarge);
    }
    if !status.success() {
        return Err(InlineDiffError::Git);
    }
    Ok(bytes)
}

fn reap_after_kill(
    child: &mut std::process::Child,
    kill: impl FnOnce(&mut std::process::Child) -> std::io::Result<()>,
) -> std::io::Result<std::process::ExitStatus> {
    // Exit can race termination (including a preceding group signal). Never
    // propagate a kill error before waiting and reaping the owned child.
    let _ = kill(child);
    child.wait()
}

pub(crate) fn validate(diff: &DiffRef) -> Result<(), InlineDiffError> {
    let id = diff.repo_id.as_bytes();
    let valid = !id.is_empty()
        && id.len() <= 64
        && id[0].is_ascii_alphanumeric()
        && id
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(b))
        && [&diff.base_sha, &diff.head_sha].iter().all(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
    if valid {
        Ok(())
    } else {
        Err(InlineDiffError::InvalidDiffRef)
    }
}

#[cfg(test)]
#[path = "prompt_git_tests.rs"]
mod tests;
