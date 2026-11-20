fn merge_result(outcome: &str, main_head: String, detail: String) -> MergeResult {
    MergeResult {
        outcome: outcome.to_string(),
        sha: None,
        main_head,
        detail,
    }
}

/// Files `branch` changed since `base`, judged against the write set.
fn confinement_violations(
    repo: &Path,
    base: &str,
    branch: &str,
    spec: &ConfineSpec,
) -> Result<Vec<confine::Violation>, mergelock::MergeError> {
    let range = format!("{base}..{branch}");
    let raw = git_bytes(repo, &["diff", "--name-only", "-z", "--no-renames", &range])?;
    let files: Vec<String> = raw
        .split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect();
    confine::evaluate(files.iter().map(String::as_str), &spec.allow, &spec.forbid)
        .map_err(|e| mergelock::MergeError::Confine(e.to_string()))
}

/// Everything that happens under the keeper lock and the file lock.
fn merge_fenced(
    cfg: &Config,
    input: &MergeInput,
    fence: &Fence,
    started: Instant,
) -> Result<MergeResult, mergelock::MergeError> {
    let repo = Path::new(&input.repo);
    let main_ref = format!("refs/heads/{}", input.main_branch);
    let lock = mergelock::MergeLock::acquire(repo, MERGE_LOCK_TIMEOUT)?;
    let head_of_main = || git(repo, &["rev-parse", &main_ref]);

    // Fencing: the repo side remembers the highest token it admitted, so a
    // holder whose keeper lock expired cannot land a late merge — provided the
    // keeper itself issues strictly monotonic tokens (stub in tests; the real
    // lock endpoint is a RELEASE-DEPLOY gap). The token is
    // always the one the keeper granted this merge; a caller cannot name one.
    let token = fence.token;
    if !mergelock::admit_fence(&lock, repo, token)? {
        return Ok(merge_result(
            "merge_refused",
            head_of_main()?,
            format!("stale fencing token {token}"),
        ));
    }

    // Main moved since the attempt branched: rebase the attempt onto it
    // (inside its own worktree) instead of throwing the work away.
    let mut base = input.base_sha.clone();
    let wt = Path::new(&input.worktree);
    let head = head_of_main()?;
    let mut rebased = false;
    if head != base && !input.worktree.is_empty() && wt.is_dir() {
        if !mergelock::rebase_onto(wt, &head)? {
            return Ok(merge_result("conflict", head, String::new()));
        }
        base = head;
        rebased = true;
    }

    // The write set is judged again here, against what will really land.
    if let Some(spec) = &input.confine {
        let v = confinement_violations(repo, &base, &input.branch, spec)?;
        if !v.is_empty() {
            return Ok(merge_result(
                "contract_defect",
                head_of_main()?,
                format!("{v:?}"),
            ));
        }
    }

    // A rebased tree is code nobody ran: check it once before it lands.
    if let (true, Some(cmd)) = (rebased, input.check_cmd.as_deref()) {
        // The check gets what is left of the merge budget, not a fresh
        // start_to_close: the blocking thread cannot be cancelled from outside,
        // so the deadline has to be honoured in here.
        let left = merge_work_budget(cfg).saturating_sub(started.elapsed());
        if left.is_zero() {
            return Ok(merge_result(
                "check_fail",
                head_of_main()?,
                "merge budget exhausted before the post-rebase check".to_string(),
            ));
        }
        let r = check::run_check(cmd, wt, left, cfg.check_tail_lines)?;
        if !r.passed() {
            let tail = if r.timed_out {
                format!(
                    "post-rebase check exceeded the merge budget
{}",
                    r.tail
                )
            } else {
                r.tail
            };
            return Ok(merge_result("check_fail", head_of_main()?, tail));
        }
    }

    let outcome = mergelock::merge_locked(&lock, repo, &input.main_branch, &input.branch, &base)?;
    Ok(map_merge(outcome, head_of_main()?))
}

/// The keeper lock owner id of this worker process: pid plus a random suffix
/// drawn once at first use, so two workers on different hosts (same pid) or one
/// restarted worker never share an id. No host name goes into it or the logs.
pub(crate) fn merge_owner_id() -> &'static str {
    use std::hash::{BuildHasher, Hasher};
    static ID: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    ID.get_or_init(|| {
        // RandomState is seeded from the OS per instance; two draws give 128 bits.
        let draw = || {
            std::collections::hash_map::RandomState::new()
                .build_hasher()
                .finish()
        };
        format!(
            "pg-worker-{}-{:016x}{:016x}",
            std::process::id(),
            draw(),
            draw()
        )
    })
}

/// Merge `branch` into `main_branch`: keeper lock, then fencing + file lock,
/// then rebase / confinement / check as needed, then fast-forward. The keeper
/// lock is released on every path; a keeper that cannot grant it means
/// `retry_later`, never a merge without it. A refused release after the merge
/// landed does not undo it (the repo-side fence and file lock guarded the
/// update). Scope, stated honestly: the file lock plus the local CAS is the
/// exclusion that holds today, over one worker host per repo; the keeper side
/// only counts once a real keeper implements the lock endpoint.
pub(crate) fn do_merge(
    keeper: &dyn Keeper,
    cfg: &Config,
    input: &MergeInput,
) -> Result<MergeResult, mergelock::MergeError> {
    // The merge budget starts here, before the keeper lock is even asked for.
    let started = Instant::now();
    let repo = Path::new(&input.repo);
    let owner = merge_owner_id();
    let fence = match keeper.acquire_merge_lock(&input.repo, owner, merge_lock_ttl_s(cfg)) {
        Ok(f) => f,
        Err(e) => {
            let main_head = git(repo, &["rev-parse", &input.main_branch])?;
            return Ok(merge_result("retry_later", main_head, e.to_string()));
        }
    };
    // Released on every exit, including an early `?` or a panic in the merge.
    let _lease = KeeperLease {
        keeper,
        repo: &input.repo,
        fence,
    };
    merge_fenced(cfg, input, &_lease.fence, started)
}

/// The keeper-held merge lock, released on drop.
///
/// A refused release (stale token, keeper down) cannot undo or change what the
/// repo-side fence and file lock already decided, so it does not fail the
/// merge; it is logged, and the keeper's own ttl reclaims a lock it never saw
/// released.
struct KeeperLease<'a> {
    keeper: &'a dyn Keeper,
    repo: &'a str,
    fence: Fence,
}

impl Drop for KeeperLease<'_> {
    fn drop(&mut self) {
        if let Err(e) = self.keeper.release_merge_lock(self.repo, &self.fence) {
            eprintln!(
                "merge lock release for {} (token {}) failed: {e}",
                self.repo, self.fence.token
            );
        }
    }
}

/// Sweep dead attempts of `input.card` (every card when empty), treating none
/// as live, and report how many were removed. Every attempt branched from the same `base_sha`.
pub(crate) fn do_janitor(cfg: &Config, input: &JanitorInput) -> Result<u32, GitError> {
    let base = input.base_sha.clone();
    let base_for = move |_: &AttemptId| base.clone();
    let spec = janitor::ProbeSpec {
        check_cmd: &input.check_cmd,
        timeout: cfg.start_to_close,
        tail_lines: cfg.check_tail_lines,
        base_for: &base_for,
    };
    let live = HashSet::new();
    let repo = Path::new(&input.repo);
    let report = if input.card.is_empty() {
        janitor::sweep(repo, &live, &spec)?
    } else {
        janitor::sweep_card(repo, &input.card, &live, &spec)?
    };
    Ok(report.removed.len() as u32)
}
