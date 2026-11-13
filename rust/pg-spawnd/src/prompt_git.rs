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
    child: std::process::Child,
    cap: usize,
    timeout: Duration,
) -> Result<Vec<u8>, InlineDiffError> {
    collect_diff_with_clock(child, cap, timeout, Instant::now, std::thread::sleep)
}

// Clock/sleep injection proves the EOF bound without scheduler-dependent timing.
fn collect_diff_with_clock(
    child: std::process::Child,
    cap: usize,
    timeout: Duration,
    now: impl Fn() -> Instant,
    sleep: impl Fn(Duration),
) -> Result<Vec<u8>, InlineDiffError> {
    collect_diff_with_poll(
        child,
        cap,
        timeout,
        now,
        sleep,
        std::process::Child::try_wait,
    )
}

fn collect_diff_with_poll(
    mut child: std::process::Child,
    cap: usize,
    timeout: Duration,
    now: impl Fn() -> Instant,
    sleep: impl Fn(Duration),
    mut poll: impl FnMut(&mut std::process::Child) -> std::io::Result<Option<std::process::ExitStatus>>,
) -> Result<Vec<u8>, InlineDiffError> {
    let deadline = now() + timeout;
    let mut exited = None;
    // Capture every collection error before cleanup, including missing stdout
    // and either try_wait observation. Child's Drop does not kill or reap it.
    let collected = (|| {
        let mut stdout = child.stdout.take().ok_or(InlineDiffError::Git)?;
        let (tx, rx) = mpsc::sync_channel(1);
        let reader = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = stdout
                .by_ref()
                .take(cap.saturating_add(1) as u64)
                .read_to_end(&mut bytes);
            // Stop at cap+1: an oversized healthy producer need not reach EOF.
            // The collector terminates/reaps its owned child before returning.
            drop(stdout);
            let _ = tx.send(result.map(|_| bytes));
        });
        let result = rx.recv_timeout(deadline.saturating_duration_since(now()));
        exited = poll(&mut child).map_err(|_| InlineDiffError::Git)?;
        let bytes = result
            .map_err(|_| InlineDiffError::Git)?
            .map_err(|_| InlineDiffError::Git)?;
        if bytes.len() > cap {
            return Err(InlineDiffError::TooLarge);
        }
        let completed = wait_before_deadline(deadline, now, sleep, || {
            exited = poll(&mut child)?;
            Ok(exited.is_some() && reader.is_finished())
        })
        .map_err(|_| InlineDiffError::Git)?;
        // An escaped descendant may still hold stdout. Detach on failure; do
        // not wait for its EOF or grant another grace period after the deadline.
        if !completed {
            return Err(InlineDiffError::Git);
        }
        // is_finished makes this join nonblocking within the original budget.
        reader.join().map_err(|_| InlineDiffError::Git)?;
        Ok(bytes)
    })();
    let status = reap_after_kill(&mut child, |child| {
        // This group was created by this invocation; never signal a registry process.
        // A failed cleanup poll is not evidence of exit: still terminate before wait.
        if exited.is_none() && !matches!(poll(child), Ok(Some(_))) {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            return child.kill();
        }
        Ok(())
    })
    .map_err(|_| InlineDiffError::Git)?;
    let bytes = collected?;
    if !status.success() {
        return Err(InlineDiffError::Git);
    }
    Ok(bytes)
}

fn wait_before_deadline(
    deadline: Instant,
    now: impl Fn() -> Instant,
    sleep: impl Fn(Duration),
    mut complete: impl FnMut() -> std::io::Result<bool>,
) -> std::io::Result<bool> {
    loop {
        // One sample per observation; never retime an accepted completion.
        let observed = now();
        if complete()? {
            return Ok(observed < deadline);
        }
        let remaining = deadline.saturating_duration_since(observed);
        if remaining.is_zero() {
            return Ok(false);
        }
        sleep(remaining.min(Duration::from_millis(5)));
    }
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
