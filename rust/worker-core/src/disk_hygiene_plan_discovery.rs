//! Candidate discovery and bounded ranking measurements for disk hygiene.
use super::*;
use crate::disk_hygiene::Device;
use crate::{
    cargo_cache::ExclusiveLease,
    cargo_pool_trim::{remove_guarded_if, stamp},
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
                .step(&item.path, item.cutoff, item.identity.0, uid)?;
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
    pub cap: u64,
}

impl HygieneReport {
    pub fn keep_boundary(&mut self, path: PathBuf, reason: &'static str) {
        self.summary.alarm |= reason == "root_unavailable";
        let device = path.metadata().ok().map(|meta| meta.dev());
        self.kept.push(KeptPath {
            device,
            path,
            bytes: None,
            bytes_is_lower_bound: false,
            reason,
        });
    }
}

impl Layout<'_> {
    pub fn discover(
        &self,
        report: &mut HygieneReport,
        budget: &Budget<'_>,
        thresholds: crate::disk_hygiene::Thresholds,
        sample: impl Fn(&Path) -> io::Result<crate::disk_runtime::DiskSpace>,
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
        let idle = now - Duration::from_secs(1800);
        let main_idle = now - crate::main_target::IDLE_AFTER;
        let legacy = now - crate::idle_targets::IDLE_AFTER;
        let mut found = vec![];
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
                for name in ["target", "node_modules"] {
                    let target = tree.join(name);
                    if target.symlink_metadata().is_ok() {
                        let mut item = Candidate::new(target, boundary.clone(), legacy)?;
                        item.scope = tree.clone();
                        found.push(item);
                    }
                }
            }
        }
        let excluded = crate::cargo_pool::hygiene_exclusions(self.shared, self.home);
        let cache = std::env::var_os("XDG_CACHE_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .unwrap_or_else(|| self.home.join(".cache"));
        let legacy_roots = crate::idle_targets::LEGACY_CONTAINERS.map(|path| self.home.join(path));
        for root in self.scratch.iter().chain([&cache]).chain(&legacy_roots) {
            let Ok(boundary) = root.canonicalize() else {
                continue;
            };
            if self.scratch.contains(root) {
                found.extend(crate::affected_scratch::discover(
                    &boundary,
                    &excluded,
                    &mut permit,
                    idle,
                    crate::affected_scratch::is_run,
                )?);
            }
            let (targets, complete) =
                crate::idle_targets::targets_in(&boundary, 2, &excluded, &mut permit)?;
            if !complete {
                report.keep_boundary(boundary.clone(), "discovery_budget");
            }
            for target in targets {
                let historical = legacy_roots.iter().any(|root| target.starts_with(root));
                let age = if historical { legacy } else { idle };
                let parent = boundary
                    .parent()
                    .filter(|_| target == boundary)
                    .unwrap_or(&boundary);
                let mut item = Candidate::new(target, parent.to_path_buf(), age)?;
                item.maintenance = historical;
                found.push(item);
            }
        }
        let parent = self.shared.first().and_then(|root| root.parent());
        if let Some(path) = parent.and_then(|root| root.canonicalize().ok()) {
            let marked = crate::private_targets::is_preflight_target;
            found.extend(crate::affected_scratch::discover(
                &path,
                &excluded,
                &mut permit,
                idle,
                marked,
            )?);
        }
        self.pool_candidates(&mut found, &mut report.summary, now, &mut permit)?;
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
            let limits = thresholds.for_filesystem(space.total);
            report
                .devices
                .insert(id, Device::new(id, root, Some(space.free), limits));
        }
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
            let leased = crate::cargo_pool::hygiene_slot_leased(&item.scope, state)?;
            let recognized =
                crate::shared_target::debug_artifacts(&item.scope)?.contains(&item.path);
            return Ok(leased
                .then_some("leased")
                .or_else(|| (!recognized).then_some("unrecognized")));
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
            cargo_pool::{pool_slot_sets, PoolCap, DEFAULT_POOL_SLOTS},
            cargo_pool_trim::trusted_slot,
            shared_target::{debug_artifacts, STALE_AFTER},
        };
        let fallback = self
            .shared
            .first()
            .and_then(|path| crate::cargo_cache::review_fallback_primary(path, self.home));
        for (index, primary) in self.shared.iter().chain(fallback.iter()).enumerate() {
            let (ordinary, review) = if index == 0 || index >= self.shared.len() {
                pool_slot_sets(primary, summary)
            } else {
                (vec![primary.clone()], vec![])
            };
            let count = ordinary.len() as u64;
            let ordinary = if index >= self.shared.len() {
                vec![]
            } else {
                ordinary
            };
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
                    for path in debug_artifacts(&root)? {
                        if !permit() {
                            return Ok(());
                        }
                        let mut item = Candidate::new(path, root.clone(), now - STALE_AFTER)?;
                        item.scope = root.clone();
                        item.lease_root = root.clone();
                        item.maintenance = true;
                        item.cap = Some(PoolCap(self.cap, total_limit, group.clone()));
                        found.push(item);
                    }
                }
            }
        }
        Ok(())
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
