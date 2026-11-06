//! Event loop for an already confined command; intentionally not wired to RL14.
use super::{unix::UnixChild, *};
#[cfg(test)]
#[path = "watch_supervise_tests.rs"]
mod tests;
use crate::{
    activity::{Act, Kind},
    events::LineKind,
};
use std::{
    io::Read,
    os::unix::process::ExitStatusExt,
    process::{Command, Stdio},
    sync::mpsc::{self, Receiver, SyncSender},
    thread,
    time::{Duration, Instant},
};

enum Chunk {
    Data(bool, Vec<u8>),
    Eof(bool),
    Error(io::Error),
}
fn reader(mut input: impl Read + Send + 'static, stdout: bool, tx: SyncSender<Chunk>) {
    thread::spawn(move || {
        let mut buf = [0; 8192];
        loop {
            let message = match input.read(&mut buf) {
                Ok(0) => {
                    let _ = tx.send(Chunk::Eof(stdout));
                    break;
                }
                Ok(n) => Chunk::Data(stdout, buf[..n].to_vec()),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => Chunk::Error(e),
            };
            let failed = matches!(message, Chunk::Error(_));
            if tx.send(message).is_err() || failed {
                break;
            }
        }
    });
}

struct Events {
    last_byte: u64,
    output: Vec<u8>,
    eof: u32,
    provider: bool,
    late: Option<u64>,
    blind: Option<BlindReason>,
    partial: bool,
}
impl Events {
    fn feed(&mut self, hooks: &mut Hooks<'_>, bytes: &[u8], now: u64, stopping: bool) {
        if let Some(last) = bytes.last() {
            self.partial = *last != b'\n';
        }
        // Mirror parser counter deltas, without interpreting payloads a second time.
        let before = hooks.parser.health();
        let old = (before.bad(), before.parsed(), before.unknown());
        for signal in hooks.parser.feed(bytes, now) {
            if stopping && (signal.kind.is_strong() || signal.kind == Kind::Request) {
                self.late.get_or_insert(now);
            }
            self.provider |= signal.kind == Kind::Error && self.blind.is_none();
            hooks.monitor.feed(signal);
        }
        let health = hooks.parser.health();
        for (kind, count) in [
            (LineKind::NotJson, health.bad().saturating_sub(old.0)),
            (
                LineKind::Known,
                health
                    .parsed()
                    .saturating_sub(old.1)
                    .saturating_sub(health.unknown().saturating_sub(old.2)),
            ),
            (LineKind::Unknown, health.unknown().saturating_sub(old.2)),
        ] {
            for _ in 0..count {
                hooks.channel.line(kind, now);
            }
        }
    }
    fn drain(
        &mut self,
        rx: &Receiver<Chunk>,
        hooks: &mut Hooks<'_>,
        now: u64,
        stopping: bool,
        max: usize,
    ) -> io::Result<()> {
        // Twice the queue capacity: see queued signals without starving clock checks.
        self.blind = self.blind.or(hooks.channel.blind(now));
        if self.blind.is_some() {
            self.provider = false;
        }
        for _ in 0..256 {
            match rx.try_recv() {
                Ok(Chunk::Data(stdout, bytes)) => {
                    self.last_byte = now;
                    self.output.extend_from_slice(&bytes);
                    if self.output.len() > max {
                        self.output.drain(..self.output.len() - max);
                    }
                    if stdout {
                        self.feed(hooks, &bytes, now, stopping);
                    }
                }
                Ok(Chunk::Eof(stdout)) => {
                    self.eof += 1;
                    // Complete a final non-newline-terminated frame exactly once.
                    if stdout && self.partial {
                        self.feed(hooks, b"\n", now, stopping);
                    }
                }
                Ok(Chunk::Error(e)) => {
                    self.blind = self.blind.or(hooks.channel.blind(now));
                    if self.blind.is_some() {
                        self.provider = false;
                    }
                    return Err(e);
                }
                Err(_) => break,
            }
        }
        self.blind = self.blind.or(hooks.channel.blind(now));
        if self.blind.is_some() {
            self.provider = false;
        }
        Ok(())
    }
}

/// Supervise the original jail-wrapped command without reconstructing it.
pub fn supervise_events(cmd: Command, opts: &stall::Opts, hooks: Hooks<'_>) -> io::Result<Watched> {
    supervise_with_writer(cmd, opts, hooks, super::unix::stdin_writer)
}

fn supervise_with_writer(
    mut cmd: Command,
    opts: &stall::Opts,
    mut hooks: Hooks<'_>,
    writer_setup: impl FnOnce(std::process::ChildStdin, &[u8]) -> io::Result<super::unix::Writer>,
) -> io::Result<Watched> {
    if opts.timeout.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "watch requires a wall timeout",
        ));
    }
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = UnixChild::spawn(&mut cmd)?;
    drop(cmd);
    let start = Instant::now();
    let (tx, rx) = mpsc::sync_channel(128);
    reader(
        child.child.stdout.take().expect("piped stdout"),
        true,
        tx.clone(),
    );
    reader(
        child.child.stderr.take().expect("piped stderr"),
        false,
        tx.clone(),
    );
    let writer = writer_setup(child.child.stdin.take().expect("piped stdin"), hooks.stdin);
    let (writer, setup_error) = match writer {
        Ok(writer) => (Some(writer), None),
        Err(error) => (None, Some(error)),
    };
    drop(tx);
    let mut events = Events {
        last_byte: 0,
        output: Vec::new(),
        eof: 0,
        provider: false,
        late: None,
        blind: None,
        partial: false,
    };
    let (mut decisions, mut stop, mut stopped_at) = (Vec::new(), None, None);
    let (mut timed_out, mut cancelled, mut stall_bytes) = (false, false, false);
    let (mut status, mut error, mut proof) = (None, setup_error, KillProof::NotNeeded);
    let mut ctx = Ctx::default();
    let mut sample_at = 0;
    let mut finished_at = None;
    loop {
        let now = start.elapsed().as_millis() as u64;
        if let Err(e) = events.drain(&rx, &mut hooks, now, stop.is_some(), opts.max_output) {
            error.get_or_insert(e);
        }
        match child.try_wait() {
            Ok(s) => status = status.or(s),
            Err(e) => {
                error.get_or_insert(e);
            }
        }
        if stop.is_none() {
            if now >= sample_at {
                ctx = (hooks.tree_sample)();
                sample_at = now.saturating_add(30_000);
            }
            if let Some(cpu) = hooks.cpu.as_mut() {
                if let Some(signal) = cpu(now) {
                    hooks.monitor.feed(signal);
                }
            }
            cancelled = status.is_none() && (hooks.cancelled)();
            timed_out = status.is_none()
                && !cancelled
                && opts.timeout.is_some_and(|t| start.elapsed() >= t);
            stall_bytes = status.is_none()
                && !cancelled
                && !timed_out
                && events.blind.is_some()
                && opts.stall.is_some_and(|t| {
                    Duration::from_millis(now.saturating_sub(events.last_byte)) >= t
                });
            let mut activity_kill = false;
            if !cancelled
                && !timed_out
                && !stall_bytes
                && !events.provider
                && status.is_none()
                && error.is_none()
            {
                if let Some(mut decision) = hooks.monitor.tick(now, ctx) {
                    if events.blind.is_some() {
                        decision.act = match decision.act {
                            Act::Warn => Act::WouldWarn,
                            Act::Kill => Act::WouldKill,
                            other => other,
                        };
                    }
                    activity_kill = decision.act == Act::Kill;
                    decisions.push(decision);
                }
            }
            if cancelled
                || timed_out
                || stall_bytes
                || events.provider
                || activity_kill
                || error.is_some()
            {
                stop = Some(Stop::default());
                stopped_at = Some(now);
            } else if status.is_some() && finished_at.is_none() {
                match child.empty() {
                    Ok(true) => {
                        finished_at.get_or_insert(now);
                    }
                    Ok(false) => {
                        stop = Some(Stop::default());
                        stopped_at = Some(now);
                    }
                    Err(e) => {
                        error.get_or_insert(e);
                        stop = Some(Stop::default());
                        stopped_at = Some(now);
                    }
                }
            }
        }
        if let Some(stop) = stop.as_mut() {
            if finished_at.is_none() {
                if let Some(result) =
                    stop.tick(&mut child, now, opts.grace.as_millis() as u64, 2000)
                {
                    proof = result; // Survivors is terminal, never retry or infer success from root exit.
                    finished_at = Some(now);
                }
                status = status.or(stop.status);
            }
        }
        if finished_at.is_some_and(|t| events.eof == 2 || now.saturating_sub(t) >= 2000) {
            if stop.is_none() {
                match child.empty() {
                    Ok(true) => {}
                    result => {
                        retain_error(&mut error, result);
                        stop = Some(Stop::default());
                        stopped_at = Some(now);
                        finished_at = None;
                        continue;
                    }
                }
            }
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    // Workers own only bounded chunks/payload. Never join a pipe held by an escaped child.
    drop(rx);
    Ok(record_writer_result(
        Watched {
            io_error: error,
            run: stall::Supervised {
                code: status.and_then(|s| s.code()),
                signal: status.and_then(|s| s.signal()),
                stalled: stall_bytes,
                timed_out,
                cancelled,
                output: events.output,
            },
            decisions,
            activity: hooks.monitor.first_activity_ms(),
            channel: ChannelSummary {
                blind: events.blind,
                lines: hooks.channel.lines(),
                bad: hooks.channel.bad(),
                blind_events: u32::from(events.blind.is_some()),
            },
            proof,
            stall_bytes,
            late_signal_ms: events.late.filter(|t| stopped_at.is_some_and(|s| *t >= s)),
        },
        writer.map_or(Ok(()), |writer| writer.finish()),
    ))
}

fn record_writer_result(mut watched: Watched, result: io::Result<()>) -> Watched {
    if let Err(error) = result {
        watched.io_error.get_or_insert(error);
    }
    watched
}

fn retain_error(error: &mut Option<io::Error>, result: io::Result<bool>) {
    if let Err(e) = result {
        error.get_or_insert(e);
    }
}
