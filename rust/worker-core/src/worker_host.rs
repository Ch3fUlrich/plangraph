//! Coordinator-owned placement state for a worker's link and rejoin checks.

use crate::{
    fleet_series::valid_host,
    lockfile::{self, LockGuard},
    run::record_io::{atomic_write_private, ensure_private_dir},
};
use serde::{Deserialize, Serialize};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

const REJOIN_AFTER_SECONDS: u64 = 10 * 60;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    #[default]
    Ready,
    Unreachable,
    Rejoining,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Step {
    Sync,
    Install,
    Runs,
    Worktrees,
    Capacity,
    Legs,
}

impl Step {
    pub const ALL: [Self; 6] = [
        Self::Sync,
        Self::Install,
        Self::Runs,
        Self::Worktrees,
        Self::Capacity,
        Self::Legs,
    ];
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct State {
    phase: Phase,
    missed: u32,
    outage_since: Option<u64>,
    observed_at: u64,
    episode: u64,
    maintenance_until: Option<u64>,
    rejoin: Option<Rejoin>,
}

#[derive(Deserialize, Serialize)]
struct Rejoin {
    started_at: u64,
    completed: Vec<(Step, serde_json::Value)>,
    failures: Vec<(Step, String)>,
    ready: bool,
}

pub struct Store {
    path: PathBuf,
}

pub fn placement_refusal(root: &Path, host: &str) -> Option<&'static str> {
    Store::new(root, host)
        .and_then(|store| store.refusal())
        .unwrap_or(Some("worker host state unavailable"))
}

impl Store {
    pub fn new(root: &Path, host: &str) -> io::Result<Self> {
        if !valid_host(host) {
            return Err(io::Error::other("invalid worker host id"));
        }
        Ok(Self {
            path: root.join("worker-hosts").join(format!("{host}.json")),
        })
    }

    fn load(&self) -> io::Result<State> {
        match fs::read(&self.path) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(State::default()),
            Err(error) => Err(error),
        }
    }

    fn save(&self, state: &State) -> io::Result<()> {
        atomic_write_private(&self.path, &serde_json::to_vec(state)?)
    }

    fn lock(&self, extension: &str) -> io::Result<LockGuard> {
        let path = self.path.with_extension(extension);
        ensure_private_dir(path.parent().expect("worker hosts directory"))?;
        lockfile::try_lock(&path)
            .map_err(io::Error::other)?
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::WouldBlock, "worker host update in progress")
            })
    }

    pub fn phase(&self) -> io::Result<Phase> {
        self.load().map(|state| state.phase)
    }

    pub fn refusal(&self) -> io::Result<Option<&'static str>> {
        Ok(match self.phase()? {
            Phase::Ready => None,
            Phase::Unreachable => Some("worker host unreachable"),
            Phase::Rejoining => Some("worker host rejoining"),
        })
    }

    /// Persist a completed observation from the coordinator's minute probe.
    pub fn observe(
        &self,
        now: u64,
        reachable: bool,
        maintenance_until: Option<u64>,
    ) -> io::Result<Phase> {
        let _lock = self.lock("lock")?;
        let mut state = self.load()?;
        if now < state.observed_at {
            return Err(io::Error::other(
                "worker host observation clock moved backwards",
            ));
        }
        let maintenance = maintenance_until
            .or(state.maintenance_until)
            .filter(|until| *until > now);
        if maintenance.is_some() && state.maintenance_until != maintenance {
            state.episode = state.episode.saturating_add(1);
        }
        state.maintenance_until = maintenance;
        if !reachable {
            if state.outage_since.is_none() {
                state.episode = state.episode.saturating_add(1);
            }
            state.outage_since.get_or_insert(now);
            state.missed = state.missed.saturating_add(1);
        }
        let rejoin_required = maintenance.is_some()
            || state.phase == Phase::Rejoining
            || state
                .outage_since
                .is_some_and(|since| now.saturating_sub(since) > REJOIN_AFTER_SECONDS);
        state.phase = if rejoin_required {
            Phase::Rejoining
        } else if reachable || state.missed < 2 {
            Phase::Ready
        } else {
            Phase::Unreachable
        };
        if reachable {
            state.outage_since = None;
            state.missed = 0;
        }
        state.observed_at = now;
        self.save(&state)?;
        Ok(state.phase)
    }

    /// Keep placement blocked until this attempt passes for the same outage episode.
    pub fn rejoin(
        &self,
        now: u64,
        mut check: impl FnMut(Step) -> Result<serde_json::Value, String>,
    ) -> io::Result<()> {
        let _attempt = self.lock("rejoin.lock")?;
        let episode = {
            let _lock = self.lock("lock")?;
            let state = self.load()?;
            if state.phase != Phase::Rejoining
                || state.outage_since.is_some()
                || state.maintenance_until.is_some()
                || now < state.observed_at
            {
                return Err(io::Error::other(
                    "worker must be rejoining and reachable outside maintenance",
                ));
            }
            state.episode
        };
        let mut failures = Vec::new();
        let mut completed = Vec::new();
        for step in Step::ALL {
            // Run settlement is useful even while installation needs repair.
            if !failures.is_empty() && step != Step::Runs {
                continue;
            }
            match check(step) {
                Ok(evidence) => completed.push((step, evidence)),
                Err(error) => failures.push((step, error)),
            }
        }
        let _lock = self.lock("lock")?;
        let mut state = self.load()?;
        let current = state.episode == episode
            && state.outage_since.is_none()
            && state.maintenance_until.is_none()
            && state.phase == Phase::Rejoining;
        let ready = current && failures.is_empty();
        let error = failures
            .first()
            .map(|(step, error)| format!("{step:?}: {error}"));
        state.rejoin = Some(Rejoin {
            started_at: now,
            completed,
            failures,
            ready,
        });
        if ready {
            state.phase = Phase::Ready;
        }
        self.save(&state)?;
        if ready {
            Ok(())
        } else {
            Err(io::Error::other(error.unwrap_or_else(|| {
                "worker outage changed during rejoin".into()
            })))
        }
    }
}
