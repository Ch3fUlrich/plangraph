//! Candidate discovery and bounded ranking measurements for disk hygiene.
use super::*;
use crate::{
    affected_scratch,
    cargo_cache::ExclusiveLease,
    cargo_pool_trim::{remove_guarded_if, stamp},
    disk_hygiene::Device,
    idle_targets,
};
use std::{os::unix::fs::MetadataExt, time::SystemTime};
pub(super) fn rank_candidate(
    row: &mut Ranked,
    item: &Candidate,
    budget: &Budget<'_>,
    uid: u32,
    deadline: Instant,
    left: &mut usize,
    resuming: bool,
) {
    let tick = (budget.clock)();
    let end = deadline.min(tick + budget.candidate_for.saturating_sub(row.spent));
    let result = (|| {
        item.safe(uid)?;
        let _lease = item.lease()?;
        if let Some(observed) = row.walk.root_stamp() {
            let current = fs::symlink_metadata(&item.path).map_err(|_| "probe_unavailable")?;
            if stamp(&current) != observed {
                row.walk = ArtifactWalk::default();
            }
        }
        if resuming {
            match row.walk.unchanged(|| (budget.clock)() < end) {
                Some(false) => row.walk = ArtifactWalk::default(),
                Some(true) if row.walk.root_stamp().is_none() => row.walk = ArtifactWalk::default(),
                Some(true) => (),
                None => {
                    row.spent = budget.candidate_for;
                    return Ok(());
                }
            }
        }
        let count = budget
            .slice_entries
            .min(budget.candidate_entries - row.work)
            .min(*left);
        for _ in 0..count {
            if row.walk.complete || (budget.clock)() >= end {
                break;
            }
            row.measured = true;
            row.work += 1;
            *left -= 1;
            row.walk
                .step(&item.path, item.cutoff, item.identity.0, uid, item.fixtures)?;
        }
        Ok(())
    })();
    row.reason = result.err();
    row.spent += (budget.clock)().saturating_duration_since(tick);
}
pub struct Layout<'a> {
    pub repo: &'a Path,
    pub roots: &'a [PathBuf],
    pub scratch: &'a [PathBuf],
    pub shared: &'a [PathBuf],
    pub home: &'a Path,
    pub cache: Option<&'a Path>,
    /// Where pytest makes `pg<pid>-*` run dirs (`/dev/shm`).
    pub shm: Option<&'a Path>,
    pub cap: u64,
}

impl Layout<'_> {
    pub fn discover(
        &self,
        report: &mut HygieneReport,
        budget: &Budget<'_>,
        thresholds: crate::disk_hygiene::Thresholds,
        sample: impl Fn(&Path) -> io::Result<crate::disk_runtime::DiskSpace>,
        initial: Option<&super::super::HygieneInventory>,
    ) -> io::Result<Vec<Candidate>> {
        let clock = budget.clock;
        let tick = clock();
        let end = budget.start + budget.rank_for;
        let mut left = budget.discovery_entries;
        let mut permit = || {
            let room = left != 0 && clock() < end;
            left = left.saturating_sub(1);
            room
        };
        let now = SystemTime::now();
        let idle = now - IDLE_SCRATCH;
        let main_idle = now - crate::main_target::IDLE_AFTER;
        let legacy = now - idle_targets::IDLE_AFTER;
        let mut found = vec![];
        let mut sweep = vec![];
        for (tree, locked, detached) in super::super::fast_worktrees::registered(
            self.repo,
            end.saturating_duration_since(clock()),
        )? {
            if !permit() {
                report.keep_boundary(tree, "discovery_budget");
                break;
            }
            let main = tree == self.repo;
            let root = self
                .roots
                .iter()
                .find(|root| tree != **root && tree.starts_with(root));
            let Some(boundary) = root.or(main.then_some(&tree)) else {
                continue;
            };
            let target = tree.join("rust/target");
            if target.symlink_metadata().is_ok() {
                let age = if main { main_idle } else { idle };
                let mut item = Candidate::new(target, tree.clone(), age)?;
                item.maintenance = main;
                if !main {
                    item.scope = tree.clone();
                }
                if locked {
                    report.keep_boundary(item.path, "unsafe");
                } else {
                    found.push(item);
                }
            }
            if !main && !locked && !(detached && tree.to_string_lossy().ends_with("-fast")) {
                let room = end.saturating_duration_since(clock());
                let Ok(listing) = super::super::fast_worktrees::ignored_caches(&tree, room) else {
                    report.keep_boundary(tree, "probe_unavailable");
                    continue;
                };
                for target in listing {
                    if !permit() {
                        report.keep_boundary(tree.clone(), "discovery_budget");
                        break;
                    }
                    let mut item = Candidate::new(target, boundary.clone(), legacy)?;
                    item.scope = tree.clone();
                    found.push(item);
                }
            }
        }
        let excluded = crate::cargo_pool::hygiene_exclusions(self.shared, self.home);
        let cache = self
            .cache
            .map(Path::to_path_buf)
            .or_else(|| std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from))
            .filter(|path| path.is_absolute())
            .unwrap_or_else(|| self.home.join(".cache"));
        let legacy_roots =
            idle_targets::LEGACY_CONTAINERS.map(|path| self.home.join(path).canonicalize().ok());
        let legacy_roots = legacy_roots.iter().flatten();
        for root in self
            .scratch
            .iter()
            .chain([&cache])
            .chain(legacy_roots.clone())
        {
            let Ok(boundary) = root.canonicalize() else {
                continue;
            };
            let scratch = self.scratch.contains(root);
            let mut boundary_kept = false;
            let mut keep = |path: &Path, reason| {
                if path == boundary && std::mem::replace(&mut boundary_kept, true) {
                    return;
                }
                if reason != "unrecognized" || (scratch && !affected_scratch::is_run(path)) {
                    report.keep_boundary(path.to_path_buf(), reason);
                }
            };
            let first = found.len();
            if scratch {
                found.extend(affected_scratch::discover(
                    &boundary,
                    &excluded,
                    &mut permit,
                    idle,
                    affected_scratch::is_run,
                    &mut keep,
                )?);
            }
            let (mut targets, complete) =
                idle_targets::targets_in(&boundary, 2, &excluded, &mut permit, keep)?;
            for target in initial
                .into_iter()
                .flat_map(|inventory| inventory.cargo_targets())
                .take_while(|_| permit())
            {
                if target.starts_with(&boundary)
                    && !excluded.iter().any(|root| target.starts_with(root))
                    && target.canonicalize().ok().as_deref() == Some(target)
                    && crate::private_targets::owned(target).unwrap_or(false)
                    && crate::private_targets::cargo_target(target)
                {
                    targets.push(target.to_path_buf());
                }
            }
            if !complete {
                report.keep_boundary(boundary.clone(), "discovery_budget");
            }
            for target in targets {
                let historical = legacy_roots.clone().any(|root| target.starts_with(root));
                let age = if historical { legacy } else { idle };
                let parent = boundary
                    .parent()
                    .filter(|_| target == boundary)
                    .unwrap_or(&boundary);
                let mut item = Candidate::new(target, parent.to_path_buf(), age)?;
                item.maintenance = historical;
                found.push(item);
            }
            if scratch {
                sweep.extend(first..found.len());
            }
        }
        let parent = self.shared.first().and_then(|root| root.parent());
        if let Some(path) = parent.and_then(|root| root.canonicalize().ok()) {
            let marked = crate::private_targets::is_preflight_target;
            let keep = |path: &Path, reason| report.keep_boundary(path.to_path_buf(), reason);
            found.extend(affected_scratch::discover(
                &path,
                &excluded,
                &mut permit,
                idle,
                marked,
                keep,
            )?);
        }
        self.pool_candidates(&mut found, &mut report.summary, now, &mut permit)?;
        self.nested_targets(&mut found, &mut sweep, now, &mut permit)?;
        self.shm_runs(&mut found, &mut sweep, now, &mut permit)?;
        for path in self
            .roots
            .iter()
            .chain([&self.repo.to_path_buf()])
            .chain(self.scratch)
            .chain(self.shared)
            .chain(found.iter().map(|item| &item.path))
        {
            let Ok(root) = path.canonicalize() else {
                continue;
            };
            let Ok(id) = root.metadata().map(|meta| meta.dev()) else {
                continue;
            };
            if report.devices.contains_key(&id) {
                continue;
            }
            let Ok(space) = sample(&root) else {
                report.keep_boundary(root, "root_unavailable");
                continue;
            };
            // A RAM-backed shm root is far smaller than the work disk: its floors are shares.
            let shm = self.shm.and_then(|shm| shm.canonicalize().ok());
            let limits = match shm {
                Some(shm) if root.starts_with(&shm) => crate::disk_hygiene::Thresholds {
                    alarm_free_bytes: 0,
                    cleanup_free_bytes: 0,
                },
                _ => thresholds,
            };
            let limits = limits.for_filesystem(space.total);
            report
                .devices
                .insert(id, Device::new(id, root, Some(space.free), limits));
        }
        sweep_idle(&mut found, &sweep, &report.devices, now);
        report
            .stage_durations
            .insert("discovery", clock().duration_since(tick).as_secs_f64());
        Ok(found)
    }

    pub fn protection(
        &self,
        item: &Candidate,
        state: &Path,
        end: Instant,
    ) -> io::Result<Option<&'static str>> {
        if item.cap.is_some() {
            if crate::cargo_pool_trim::hygiene_slot_leased(&item.scope, state)? {
                return Ok(Some("leased"));
            }
            return crate::main_target::hygiene_artifact(item, end);
        }
        if item.path == item.boundary.join("rust/target") {
            if !crate::main_target::identified(&item.path) {
                return Ok(Some("unrecognized"));
            }
            if !crate::main_target::ignored_rust_target(&item.boundary, end)? {
                return Ok(Some("unsafe"));
            }
        } else if item.path.starts_with(&item.scope) && item.path != item.scope {
            let git = |args: &[&str]| crate::main_target::git_until(&item.scope, args, end);
            if !git(&[
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(glob,exclude)**/*.plangraph-lock",
            ])?
            .is_empty()
            {
                return Ok(Some("unsafe"));
            }
            let rel = item
                .path
                .strip_prefix(&item.scope)
                .unwrap()
                .to_string_lossy();
            if !git(&["--literal-pathspecs", "ls-files", "--", &rel])?.is_empty()
                || !git(&["check-ignore", "--", &rel])?.ends_with(b"\n")
            {
                return Ok(Some("unsafe"));
            }
        }
        Ok(None)
    }

    fn pool_candidates(
        &self,
        found: &mut Vec<Candidate>,
        summary: &mut super::super::Report,
        now: SystemTime,
        permit: &mut impl FnMut() -> bool,
    ) -> io::Result<()> {
        use crate::{
            cargo_pool::{pool_slot_sets, DEFAULT_POOL_SLOTS},
            cargo_pool_trim::trusted_slot,
            shared_target::PoolCap,
        };
        let fallback = self
            .shared
            .first()
            .and_then(|path| crate::cargo_cache::review_fallback_primary(path, self.home));
        for (index, primary) in self.shared.iter().chain(fallback.iter()).enumerate() {
            let (mut ordinary, review) = if index == 0 || index >= self.shared.len() {
                pool_slot_sets(primary, summary)
            } else {
                (vec![primary.clone()], vec![])
            };
            let count = ordinary.len() as u64;
            if index >= self.shared.len() {
                ordinary.clear();
            }
            for (slots, count) in [(ordinary, count), (review, DEFAULT_POOL_SLOTS as u64)] {
                let group: std::sync::Arc<[PathBuf]> = slots.into();
                let total_limit = self.cap.saturating_mul(count);
                for slot in group.iter() {
                    if !permit() {
                        return Ok(());
                    }
                    let Some(root) = trusted_slot(primary, slot)? else {
                        continue;
                    };
                    let cap = PoolCap(self.cap, total_limit, group.clone());
                    // A build in a target nested in this root does not use the root's own
                    // profiles.
                    let nested = Some(nested_dirs(&root).into());
                    if !capped_artifacts(&root, cap, nested, now, found, permit)? {
                        return Ok(());
                    }
                }
            }
        }
        Ok(())
    }

    /// Cargo targets nested in a shared root (`<shared>/<name>`, an agent's own
    /// CARGO_TARGET_DIR): each is capped like a pool slot, and removed whole when idle. The
    /// indices of the whole-target candidates go to `sweep`.
    fn nested_targets(
        &self,
        found: &mut Vec<Candidate>,
        sweep: &mut Vec<usize>,
        now: SystemTime,
        permit: &mut impl FnMut() -> bool,
    ) -> io::Result<()> {
        for primary in self
            .shared
            .iter()
            .filter_map(|root| root.canonicalize().ok())
        {
            for path in nested_dirs(&primary) {
                if !permit() {
                    return Ok(());
                }
                let mut whole = Candidate::new(path.clone(), primary.clone(), now - IDLE_SCRATCH)?;
                whole.maintenance = true;
                sweep.push(found.len());
                found.push(whole);
                let group: std::sync::Arc<[PathBuf]> = vec![path.clone()].into();
                let cap = crate::shared_target::PoolCap(self.cap, self.cap, group);
                if !capped_artifacts(&path, cap, None, now, found, permit)? {
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    /// Run dirs `pg<pid>-*` that pytest made in the shm root, whose pid no longer runs.
    fn shm_runs(
        &self,
        found: &mut Vec<Candidate>,
        sweep: &mut Vec<usize>,
        now: SystemTime,
        permit: &mut impl FnMut() -> bool,
    ) -> io::Result<()> {
        let Some(root) = self.shm.and_then(|root| root.canonicalize().ok()) else {
            return Ok(());
        };
        let Ok(entries) = fs::read_dir(&root) else {
            return Ok(());
        };
        for entry in entries.flatten() {
            if !permit() {
                break;
            }
            let pid = entry.file_name().to_str().and_then(|name| {
                let (pid, rest) = name.strip_prefix("pg")?.split_once('-')?;
                let pid: libc::pid_t = pid.parse().ok().filter(|pid| *pid > 0)?;
                (!rest.is_empty()).then_some(pid)
            });
            let Some(pid) = pid else {
                continue;
            };
            // SAFETY: signal 0 only checks that the pid exists.
            let dead = unsafe { libc::kill(pid, 0) } != 0
                && io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
            if !dead || !entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                continue;
            }
            let mut item = Candidate::new(entry.path(), root.clone(), now - IDLE_SCRATCH)?;
            item.lease_root = root.join("pg-hygiene");
            item.fixtures = true;
            sweep.push(found.len());
            found.push(item);
        }
        Ok(())
    }
}

/// Profile and output directories of a Cargo target root, never a nested target.
const PROFILES: [&str; 5] = ["debug", "release", "tmp", "doc", "package"];

/// Cargo targets nested in a target root: real directories with their own target markers.
fn nested_dirs(root: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(root) else {
        return vec![];
    };
    entries
        .flatten()
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
        .filter(|entry| !PROFILES.iter().any(|name| entry.file_name() == *name))
        .map(|entry| entry.path())
        .filter(|path| crate::private_targets::cargo_target(path))
        .collect()
}
/// Scratch younger than this stays, also under disk pressure.
const IDLE_SCRATCH: Duration = Duration::from_secs(1800);
/// Scratch and nested targets idle this long go also without disk pressure.
pub const SWEEP_AFTER: Duration = Duration::from_secs(6 * 3600);

/// The profile and build-unit candidates of one capped target root (a pool slot or a nested
/// target). False when the discovery budget ran out.
fn capped_artifacts(
    root: &Path,
    cap: crate::shared_target::PoolCap,
    nested: Option<std::sync::Arc<[PathBuf]>>,
    now: SystemTime,
    found: &mut Vec<Candidate>,
    permit: &mut impl FnMut() -> bool,
) -> io::Result<bool> {
    use crate::shared_target::{debug_artifacts, STALE_AFTER};
    let mut paths = debug_artifacts(root)?;
    for name in ["debug", "release"] {
        let path = root.join(name);
        if path.is_dir() && !paths.contains(&path) {
            paths.push(path);
        }
    }
    for path in paths {
        if !permit() {
            return Ok(false);
        }
        let mut item = Candidate::new(path, root.to_path_buf(), now - STALE_AFTER)?;
        item.scope = root.to_path_buf();
        item.lease_root = root.to_path_buf();
        item.maintenance = true;
        item.cap = Some(cap.clone());
        if let Some(nested) = &nested {
            item.nested = nested.clone();
        }
        found.push(item);
    }
    Ok(true)
}

/// Above the cleanup floor a sweepable candidate goes once idle for [`SWEEP_AFTER`]; under
/// pressure it keeps its shorter cutoff and goes as rebuildable scratch.
fn sweep_idle(
    found: &mut [Candidate],
    sweep: &[usize],
    devices: &std::collections::BTreeMap<u64, Device>,
    now: SystemTime,
) {
    for &index in sweep {
        let Some(item) = found.get_mut(index) else {
            continue;
        };
        let device = devices.get(&item.identity.0);
        let calm = device.and_then(|row| Some(row.start_free_bytes? >= row.cleanup_floor_bytes));
        if calm == Some(true) {
            item.cutoff = now - SWEEP_AFTER;
            item.maintenance = true;
        } else {
            item.maintenance = false;
        }
    }
}

pub(super) fn rank_slices(
    rows: &mut [(Candidate, Ranked)],
    budget: &Budget<'_>,
    end: Instant,
    refresh: bool,
    held: Option<&Path>,
) {
    // SAFETY: geteuid has no preconditions.
    let uid = unsafe { libc::geteuid() };
    let mut left = budget.rank_entries;
    loop {
        let before = left;
        for (item, row) in &mut *rows {
            if left == 0 || (budget.clock)() >= end {
                break;
            }
            if row.reason.is_none()
                && !row.walk.complete
                && held != Some(&item.lease_root)
                && row.work < budget.candidate_entries
                && row.spent < budget.candidate_for
            {
                rank_candidate(
                    row,
                    item,
                    budget,
                    uid,
                    end,
                    &mut left,
                    refresh && row.work == 0,
                );
            }
        }
        if left == before {
            break;
        }
    }
    rows.sort_by(|(a, ra), (b, rb)| {
        rb.walk
            .bytes
            .cmp(&ra.walk.bytes)
            .then_with(|| a.path.cmp(&b.path))
    });
}

/// A candidate's freshness and ownership walk, held under its removal lease.
pub struct FinalProof {
    pub(super) item: Candidate,
    uid: u32,
    _lease: ExclusiveLease,
    walk: ArtifactWalk,
}

impl FinalProof {
    /// Acquire the nonblocking owner lease before inspecting any candidate contents.
    pub fn begin(item: Candidate) -> Result<Self, &'static str> {
        // SAFETY: geteuid has no preconditions.
        let uid = unsafe { libc::geteuid() };
        item.safe(uid)?;
        let lease = item.lease()?;
        Ok(Self {
            item,
            uid,
            _lease: lease,
            walk: ArtifactWalk::default(),
        })
    }

    /// Continue until EOF or the current deadline, restarting stale observations under the lease.
    pub fn advance(&mut self, room: impl Fn() -> bool) -> Result<bool, &'static str> {
        self.item.safe(self.uid)?;
        match self.walk.unchanged(&room) {
            Some(false) => self.walk = ArtifactWalk::default(),
            Some(true) => (),
            None => return Ok(false),
        }
        while room() && !self.walk.complete {
            self.walk.step(
                &self.item.path,
                self.item.cutoff,
                self.item.identity.0,
                self.uid,
                self.item.fixtures,
            )?;
        }
        Ok(self.walk.complete)
    }

    /// Apply a completed proof after refreshing protection. Return bytes only for an allowed action.
    pub fn remove(
        &self,
        apply: bool,
        allowed: impl FnOnce() -> io::Result<bool>,
        before_unlink: impl FnOnce() -> bool,
    ) -> io::Result<Option<u64>> {
        if !self.walk.complete {
            return Err(io::Error::other("candidate proof incomplete"));
        }
        self.item.safe(self.uid).map_err(io::Error::other)?;
        let mut authorized = Ok(false);
        let removed = remove_guarded_if(&self.item.boundary, &self.item.path, apply, |path| {
            authorized = allowed();
            matches!(authorized, Ok(true))
                && fs::symlink_metadata(path).ok().map(|meta| stamp(&meta))
                    == self.walk.root_stamp()
                && (!apply || before_unlink())
        })?;
        authorized?;
        Ok(removed.then_some(self.walk.credit))
    }
}
