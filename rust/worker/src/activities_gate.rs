#[path = "phase_branch_io.rs"]
mod phase_branch_io;
use phase_branch_io::{ensure_phase_branch_with, phase_branch_error};

/// What a gate run produced: a result, or the fact that the caller cancelled
/// it before the command finished.
#[derive(Debug)]
pub(crate) enum GateRun {
    Done(GateResult),
    Cancelled,
}

/// The name a check must carry: short, no whitespace, no path or shell
/// characters, so a report line `[name]` stays unambiguous.
fn gate_check_name_ok(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 64
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// Reject an invalid check list before ANY check runs, so a bad list can never
/// produce a partial gate. Invalid input is `ErrorKind::InvalidInput`, which
/// [`gate_error`] reports as a non-retryable application failure.
fn validate_gate_input(input: &GateInput) -> std::io::Result<()> {
    if input.checks.is_empty() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "gate has no checks",
        ));
    }
    let mut seen: HashSet<&str> = HashSet::new();
    for c in &input.checks {
        match c.builtin.as_deref() {
            Some("diffscan") => {
                let base = input.base_sha.as_deref().ok_or_else(|| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "diffscan requires base_sha",
                    )
                })?;
                pg_worker_core::git::check_rev(base).map_err(|_| {
                    std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "invalid diffscan base_sha",
                    )
                })?;
            }
            Some("exports") | None => {}
            Some(_) => {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "unknown gate builtin",
                ));
            }
        }
        if !gate_check_name_ok(&c.name) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!(
                    "gate check name {name:?} must match [A-Za-z0-9_.-]{{1,64}}",
                    name = c.name
                ),
            ));
        }
        if !seen.insert(c.name.as_str()) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("duplicate gate check name {:?}", c.name),
            ));
        }
        if c.cmd.trim().is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("gate check {:?} has an empty command", c.name),
            ));
        }
        if c.timeout_s == Some(0) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!("gate check {:?} timeout_s must be at least 1", c.name),
            ));
        }
    }
    Ok(())
}

/// Run the named checks of a card's gate sequentially, in list order, in one
/// confined directory, through the shared check runner.
///
/// Validation is up front ([`validate_gate_input`]) and the workdir is
/// confined before anything runs ([`pathjail::confine_dir`], whose escapes and
/// non-directories are `InvalidInput` as well): an empty list, a blank
/// command, a blank/duplicate/misnamed check, a `timeout_s` of 0 and a workdir
/// outside the card work root are invalid input, never a pass — and nothing
/// runs, so a bad list can never produce a partial gate. [`gate_error`] keeps
/// all of them non-retryable and the check runner is never entered with a bad
/// directory (it creates a sibling home dir).
///
/// Time is ONE shared budget of the configured `start_to_close` for the whole
/// gate: each check gets `min(its own timeout_s, what is left of the budget)`,
/// and a check that would start after the budget is spent is reported as not
/// run (`timed_out`, tail "not run: gate budget exhausted") instead of being
/// killed mid-flight.
///
/// Each check keeps its OWN bounded tail — the last `min(cfg.check_tail_lines,
/// 50)` lines, redacted and byte-capped by the check runner itself — and its
/// own `output_hash` of that tail. Nothing raw may reach Temporal history, and
/// hashing the raw output would let anyone with the hash confirm a guessed
/// secret (and would make the hash differ across runs that differ only in a
/// secret, breaking "identical failure" comparison). A failing check does not
/// stop the later ones: a later diff scan must still report.
///
/// Cancellation is unchanged: `cancelled` is polled by the check runner, which
/// kills the whole child process group, and it is checked between checks too;
/// the deadline is honoured in here because the blocking thread cannot be
/// cancelled from outside.
pub(crate) fn do_gate(
    cfg: &Config,
    input: &GateInput,
    cancelled: &dyn Fn() -> bool,
) -> std::io::Result<GateRun> {
    validate_gate_input(input)?;
    let dir = pathjail::confine_dir(Path::new(&input.workroot), &input.workdir)?;
    let started = Instant::now();
    let mut results = Vec::with_capacity(input.checks.len());
    for c in &input.checks {
        if cancelled() {
            return Ok(GateRun::Cancelled);
        }
        let remaining = cfg.start_to_close.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            // Do not start a command that must be killed mid-flight: report it
            // as not run, so the report still names every check of the gate.
            let tail = "not run: gate budget exhausted".to_string();
            results.push(GateCheckResult {
                name: c.name.clone(),
                passed: false,
                exit_code: None,
                signal: None,
                timed_out: true,
                output_hash: hash::hash_bytes(tail.as_bytes()),
                tail,
            });
            continue;
        }
        if let Some(builtin) = c.builtin.as_deref() {
            let (passed, reason) = if builtin == "diffscan" {
                let range = format!(
                    "{}..HEAD",
                    input.base_sha.as_deref().expect("validated base")
                );
                let diff = git(
                    &dir,
                    &[
                        "diff",
                        "--no-renames",
                        "--no-ext-diff",
                        "--no-textconv",
                        &range,
                        "--",
                    ],
                )
                .map_err(std::io::Error::other)?;
                let findings = pg_worker_core::diffscan::scan_diff(&diff);
                (
                    findings.is_empty(),
                    pg_worker_core::diffscan::reason(&findings),
                )
            } else {
                let missing = pg_worker_core::exports::verify(&input.exports, &|path| {
                    let candidate = dir.join(path);
                    let parent = candidate.parent()?.to_str()?;
                    let parent = pathjail::confine_dir(&dir, parent).ok()?;
                    let file = parent.join(candidate.file_name()?).canonicalize().ok()?;
                    if !file.starts_with(&dir) {
                        return None;
                    }
                    std::fs::read_to_string(file).ok()
                });
                let reason = if missing.is_empty() {
                    String::new()
                } else {
                    format!("missing exports: {}", missing.join(", "))
                };
                (missing.is_empty(), redact::cap_tail_bytes(&reason, 300))
            };
            let tail = redact::cap_tail_bytes(&redact::redact(&reason), redact::MAX_TAIL_BYTES);
            results.push(GateCheckResult {
                name: c.name.clone(),
                passed,
                exit_code: Some(if passed { 0 } else { 1 }),
                signal: None,
                timed_out: false,
                output_hash: hash::hash_bytes(tail.as_bytes()),
                tail,
            });
            continue;
        }
        let timeout = c
            .timeout_s
            .map(Duration::from_secs)
            .map_or(remaining, |t| t.min(remaining));
        // The runner redacts the WHOLE output before its line cut and caps the
        // bytes; the line cap per check is min(cfg lines, 50).
        let r = check::run_check_cancellable(
            &c.cmd,
            &dir,
            timeout,
            cfg.check_tail_lines.min(50),
            cancelled,
        )?;
        if r.cancelled {
            return Ok(GateRun::Cancelled);
        }
        // The tail is already redacted; the byte cap here is defensive only,
        // so a future runner change cannot push raw bytes into history.
        let tail = redact::cap_tail_bytes(&r.tail, redact::MAX_TAIL_BYTES);
        results.push(GateCheckResult {
            name: c.name.clone(),
            passed: r.passed(),
            exit_code: r.exit_code,
            signal: r.signal,
            timed_out: r.timed_out,
            output_hash: hash::hash_bytes(tail.as_bytes()),
            tail,
        });
    }
    Ok(GateRun::Done(GateResult {
        passed: results.iter().all(|c| c.passed),
        checks: results,
    }))
}
