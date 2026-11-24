//! Coordinator-owned placement state for a worker link and maintenance window.

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

#[path = "worker_host_liveness.rs"]
mod liveness;
pub use liveness::Observation;
#[path = "worker_host_rejoin.rs"]
mod rejoin;
pub use rejoin::{
    FloorReport, InstallReport, LegReport, RejoinError, RejoinHost, RunFate, SyncReport,
};

/// An outage longer than this holds the host in `rejoining` until the rejoin gate passes.
pub const REJOIN_AFTER_SECS: u64 = 600;

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    #[default]
    Ready,
    Unreachable,
    Rejoining,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct State {
    phase: Phase,
    missed: u32,
    outage_since: Option<u64>,
    observed_at: u64,
    #[serde(default, rename = "episode", skip_serializing)]
    _legacy_episode: Option<serde::de::IgnoredAny>,
    maintenance_until: Option<u64>,
    #[serde(default)]
    needs_rejoin: bool,
    #[serde(default, rename = "rejoin", skip_serializing)]
    _legacy_rejoin: Option<serde::de::IgnoredAny>,
    #[serde(default)]
    link: liveness::Link,
}

impl State {
    fn advance(&mut self, now: u64, reachable: bool) -> io::Result<()> {
        if now < self.observed_at {
            return Err(io::Error::other(
                "worker host observation clock moved backwards",
            ));
        }
        let was_rejoining = self.phase == Phase::Rejoining;
        self.maintenance_until = self.maintenance_until.filter(|until| *until > now);
        if !reachable {
            self.outage_since.get_or_insert(now);
            self.missed = self.missed.saturating_add(1);
        }
        if self
            .outage_since
            .is_some_and(|since| now.saturating_sub(since) > REJOIN_AFTER_SECS)
        {
            self.needs_rejoin = true;
        }
        self.phase = if self.maintenance_until.is_some() {
            Phase::Rejoining
        } else if !reachable && (self.missed >= 2 || was_rejoining) {
            Phase::Unreachable
        } else if self.needs_rejoin {
            Phase::Rejoining
        } else {
            Phase::Ready
        };
        if reachable {
            self.outage_since = None;
            self.missed = 0;
        }
        self.observed_at = now;
        Ok(())
    }
}

pub struct Store {
    path: PathBuf,
    root: PathBuf,
    host: String,
}

pub fn placement_refusal(root: &Path, host: &str) -> Option<&'static str> {
    Store::new(root, host)
        .and_then(|store| store.refusal())
        .unwrap_or(Some("worker host state unavailable"))
}

/// The coordinator's placement hold on a host name, or None for a name with no worker
/// state (the local host, or a policy name that is not a worker id).
pub fn placement_hold(root: &Path, host: &str) -> Option<&'static str> {
    valid_host(host).then(|| placement_refusal(root, host))?
}

impl Store {
    pub fn new(root: &Path, host: &str) -> io::Result<Self> {
        if !valid_host(host) {
            return Err(io::Error::other("invalid worker host id"));
        }
        Ok(Self {
            path: root.join("worker-hosts").join(format!("{host}.json")),
            root: root.into(),
            host: host.into(),
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
}
