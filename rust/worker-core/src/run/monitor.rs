use super::{
    capture::{self, Capture, CaptureCfg, Stream},
    finalize,
    group::*,
    record::{RunRecord, Status},
    scrub::Scrubber,
    supervise::*,
};
use serde_json::{json, Value};
use std::{
    io::Write,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread,
    time::{Duration, Instant},
};
pub(super) fn monitor(
    rt: Arc<Runtime>,
    mut spec: RunSpec,
    mut record: RunRecord,
    cancel: Arc<AtomicBool>,
) {
    let mut permit = None; // One run keeps its admission slot across review legs.
    let mut owned = Vec::new(); // Review slots this run allocated, one per leg at most.
    loop {
        let slot = spec
            .review_slot
            .clone()
            .filter(|slot| crate::cargo_pool::is_review_slot(slot));
        let run_id = spec.run_id.clone();
        let next = attempt(rt.clone(), spec, &mut record, cancel.clone(), &mut permit);
        owned.extend(slot);
        match next {
            Some(next) => spec = next,
            None => {
                // Only what the run allocated goes, and only once its record shows it finished.
                let finished = RunRecord::read(&rt.cfg.state_dir, &run_id)
                    .map(|r| crate::sweep_runs::finished(&r.to_value()).unwrap_or(false))
                    .unwrap_or(false);
                if finished {
                    for slot in owned {
                        let _ = crate::cargo_pool::remove_review_slot(&slot);
                    }
                }
                return;
            }
        }
    }
}
fn attempt(
    rt: Arc<Runtime>,
    mut spec: RunSpec,
    record: &mut RunRecord,
    cancel: Arc<AtomicBool>,
    permit: &mut Option<Permit>,
) -> Option<RunSpec> {
    let mut grouped: Option<Grouped> = None;
    let mut capture = None;
    #[cfg(target_os = "linux")]
    let mut slot_lease: Option<crate::cargo_cache::CacheLease> = None;
    let cancelled = || cancel.load(Ordering::SeqCst) || record_dir_cancelled(&rt, &spec.run_id);
    let result = catch_unwind(AssertUnwindSafe(|| {
        if permit.is_none() {
            // Admission can block; mirror CLI markers into the same flag it observes.
            let admitting = Arc::new(AtomicBool::new(true));
            let (active, flag, runtime, id) = (
                admitting.clone(),
                cancel.clone(),
                rt.clone(),
                spec.run_id.clone(),
            );
            let watcher = thread::spawn(move || {
                while active.load(Ordering::SeqCst) {
                    if record_dir_cancelled(&runtime, &id) {
                        flag.store(true, Ordering::SeqCst);
                    }
                    thread::sleep(Duration::from_millis(10));
                }
            });
            let admission = catch_unwind(AssertUnwindSafe(|| {
                rt.admission.acquire(&spec.run_id, &cancel)
            }));
            admitting.store(false, Ordering::SeqCst);
            let _ = watcher.join();
            let admission = admission.unwrap_or_else(|p| std::panic::resume_unwind(p));
            match admission {
                Admit::Cancelled => return (1, true, StopOutcome::Empty),
                Admit::Granted(p) => *permit = Some(p),
            }
        }
        if cancelled() {
            return (1, true, StopOutcome::Empty);
        }
        // Each leg leases its own slot before it launches; a slot hygiene is reclaiming is refused.
        #[cfg(target_os = "linux")]
        if let Some(slot) = spec
            .review_slot
            .as_deref()
            .filter(|slot| crate::cargo_pool::is_review_slot(slot))
        {
            match crate::cargo_cache::writer_lease(slot) {
                // The lease alone does not make the slot exist: admission cleanup may have
                // removed it between the adapter's preparation and this launch.
                Ok(lease) if !slot.is_dir() => {
                    drop(lease);
                    record.set("error", json!("review slot unavailable"));
                    record.set("reason", json!("review slot was removed before launch"));
                    return (1, false, StopOutcome::Empty);
                }
                Ok(lease) => slot_lease = Some(lease),
                Err(error) => {
                    record.set("error", json!("review slot unavailable"));
                    record.set("reason", json!(format!("review slot lease: {error}")));
                    return (1, false, StopOutcome::Empty);
                }
            }
        }
        let launch = LaunchSpec {
            program: &spec.program,
            args: &spec.args,
            cwd: &spec.cwd,
            env: &spec.env,
            stdin_piped: spec.stdin.is_some(),
        };
        grouped = match rt.launcher.launch(
            &unit_name(&spec.run_id),
            &launch,
            &rt.cfg.confinement,
            &rt.cfg.limits,
        ) {
            Ok(g) => Some(g),
            Err(e) => {
                launch_error(record, e);
                return (1, false, StopOutcome::Empty);
            }
        };
        let g = grouped.as_mut().unwrap();
        let identity = g.group.identity();
        #[cfg(windows)]
        record.set("holder_deadline_ms", json!(g.group.holder_deadline_ms()));
        #[cfg(windows)]
        if g.group.holder_no_breakaway() {
            let _ = record.push_event(json!({"event":"holder_no_breakaway"}));
        }
        #[cfg(unix)]
        // SAFETY: getpgid only inspects the spawned child's group membership.
        let pgid = u32::try_from(unsafe { libc::getpgid(identity.pid as i32) }).ok();
        #[cfg(not(unix))]
        let pgid: Option<u32> = None;
        record.set(
            "queued_at",
            record.get("started").cloned().unwrap_or(Value::Null),
        );
        if record
            .get("attempts")
            .and_then(Value::as_array)
            .is_none_or(|a| a.len() <= 1)
        {
            record.set("started", json!(finalize::now()));
        }
        record.set("pid", json!(identity.pid));
        record.set("pid_start", json!(identity.start_time));
        record.set("job_object", json!(cfg!(windows)));
        record.set("job_id", json!(spec.job_id));
        record.set(
            "proc",
            json!({"pid":identity.pid, "start_time":identity.start_time,
                "group":identity.group, "pgid":pgid}),
        );
        let log = record.path().with_file_name("live.log");
        record.set("live_log", json!(log));
        record.set("last_activity_ts", Value::Null);
        let _ = record.transition(Status::Running, finalize::now());
        finalize::save(record);
        let c = Arc::new(Capture::new(CaptureCfg {
            live_log: Some(log),
            live_cfg: Default::default(),
            scrubber: Scrubber::from_env_with_allowlist(&spec.env, &spec.allow_shas),
        }));
        capture = Some(c.clone());
        let mut readers = Vec::new();
        if let Some(pipe) = g.child.stdout.take() {
            let c = c.clone();
            readers.push(thread::spawn(move || {
                capture::pump(pipe, Stream::Stdout, &c)
            }));
        }
        if let Some(pipe) = g.child.stderr.take() {
            let c = c.clone();
            readers.push(thread::spawn(move || {
                capture::pump(pipe, Stream::Stderr, &c)
            }));
        }
        if let (Some(payload), Some(mut pipe)) = (spec.stdin.take(), g.child.stdin.take()) {
            thread::spawn(move || {
                let _ = pipe.write_all(&payload);
            });
        }
        let mut last = Instant::now();
        let mut seq = 0;
        let (exit, was_cancelled) = loop {
            if cancelled() {
                break (None, true);
            }
            if record.get("role").and_then(Value::as_str) == Some("review") {
                if let Some(reason) = c.review_quota_reason() {
                    record.set("reason", json!(reason));
                    break (None, false);
                }
            }
            match g.child.try_wait() {
                Ok(Some(s)) => break (Some(s), false),
                Err(_) => break (None, false),
                _ => {}
            }
            if last.elapsed() >= rt.cfg.save_every {
                let next = c.snapshot().seq;
                if next != seq {
                    finalize::output(record, &c, false);
                    if finalize::save(record) {
                        seq = next;
                    }
                }
                last = Instant::now();
            }
            thread::sleep(Duration::from_millis(50));
        };
        let stop = stop_group(
            g.group.as_ref(),
            if was_cancelled {
                rt.cfg.grace
            } else {
                Duration::ZERO
            },
            rt.cfg.kill_timeout,
        );
        let exit = exit.or_else(|| g.child.wait().ok());
        let deadline = Instant::now() + rt.cfg.reader_grace;
        while readers.iter().any(|h| !h.is_finished()) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        for h in readers {
            if h.is_finished() {
                let _ = h.join();
            }
        }
        c.flush_live();
        (
            if record
                .get("reason")
                .and_then(Value::as_str)
                .is_some_and(|s| s.starts_with("review_quota:"))
            {
                7
            } else {
                exit.map(exit_code).unwrap_or(1)
            },
            was_cancelled,
            stop,
        )
    }));
    let (code, cancelled, stop) = result.unwrap_or_else(|_| {
        let _ = record.push_event(json!({"event":"monitor_failed", "error":"panic"}));
        let stop = grouped.as_mut().map_or(StopOutcome::Empty, |g| {
            let s = stop_group(g.group.as_ref(), Duration::ZERO, rt.cfg.kill_timeout);
            let _ = g.child.wait();
            s
        });
        (1, false, stop)
    });
    let held = matches!(stop, StopOutcome::Survivors(_));
    // Cancellation can arrive while stopping the group or draining readers.
    let cancelled =
        cancelled || cancel.load(Ordering::SeqCst) || record_dir_cancelled(&rt, &spec.run_id);
    let next = finalize::finish(
        record,
        &mut spec.finish,
        code,
        cancelled,
        stop,
        capture.as_deref(),
    );
    if held {
        if let Some(g) = &grouped {
            let mut polls = 0;
            loop {
                if g.group.members().is_ok_and(|p| p.is_empty()) {
                    break;
                }
                polls += 1;
                if polls % 10 == 0 {
                    g.group.kill();
                }
                thread::sleep(rt.cfg.held_poll);
            }
        }
    }
    #[cfg(windows)]
    if let Some(g) = &grouped {
        g.group.terminate();
    }
    drop(grouped);
    #[cfg(target_os = "linux")]
    drop(slot_lease);
    next
}
fn record_dir_cancelled(rt: &Runtime, id: &str) -> bool {
    rt.cfg
        .state_dir
        .join("runs")
        .join(id)
        .join("cancelled")
        .exists()
}
fn exit_code(status: std::process::ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return -signal;
        }
    }
    1
}
