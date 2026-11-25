//! What keeps a lane head alive: keeper claims bound to branches and worktree paths, local
//! checkouts and settlement holds. An unresolved source is an error and keeps every head.
use super::lane_proof::out;
use pg_worker_core::disk_hygiene::Guards;
use serde::Deserialize;
use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// A settlement hold lapses after this long: a train that never settles must not keep a ref forever.
const HOLD_SECS: u64 = 14 * 86400;

#[derive(Deserialize, Default)]
#[serde(default)]
struct Claims {
    paths: Vec<PathBuf>,
    branches: Vec<String>,
    /// Active runs of other hosts: no local record binds them to a publish ref.
    unbound_runs: u64,
}

/// Everything that keeps a head alive. `Err` means a source is unresolved: every head is kept.
pub(super) struct Protection {
    claims: BTreeSet<String>,
    pub unbound_runs: u64,
    /// Worktrees of this host as (path, full branch ref).
    trees: Vec<(PathBuf, String)>,
    claim_paths: Vec<PathBuf>,
    holds: BTreeSet<String>,
    pub loaded: Instant,
}

pub(super) fn load_protection(
    claims_reader: Option<&str>,
    state: &Path,
    run_state: &Path,
    repo: &Path,
) -> Result<Protection, String> {
    let reader = claims_reader.ok_or("claims reader unavailable")?;
    let text = pg_worker_core::host_command::output(
        Command::new("sh")
            .env("PG_SWEEP_STATE", run_state)
            .args(["-c", reader]),
        Duration::from_secs(120),
    )
    .map_err(|e| format!("claims reader failed: {e}"))?;
    let claims: Claims =
        serde_json::from_slice(&text).map_err(|e| format!("claims unreadable: {e}"))?;
    let listing = out(repo, &["worktree", "list", "--porcelain"])?;
    let (mut trees, mut path) = (Vec::new(), None);
    for line in listing.lines() {
        if let Some(p) = line.strip_prefix("worktree ") {
            path = Some(PathBuf::from(p));
        } else if let (Some(b), Some(p)) = (line.strip_prefix("branch "), &path) {
            trees.push((p.clone(), b.to_string()));
        }
    }
    // `<ref>[<TAB><epoch seconds>]`: a stamped hold lapses after HOLD_SECS, an unstamped one never does.
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let holds = match fs::read_to_string(state.join("lane-holds.txt")) {
        Ok(text) => text
            .lines()
            .filter_map(|row| {
                let (name, stamp) = row.split_once('\t').unwrap_or((row, ""));
                let live = stamp
                    .trim()
                    .parse::<u64>()
                    .map_or(true, |at| now.saturating_sub(at) <= HOLD_SECS);
                (live && !name.trim().is_empty()).then(|| name.trim().to_string())
            })
            .collect(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => BTreeSet::new(),
        Err(e) => return Err(e.to_string()),
    };
    Ok(Protection {
        claims: claims.branches.into_iter().collect(),
        unbound_runs: claims.unbound_runs,
        trees,
        claim_paths: claims.paths,
        holds,
        loaded: Instant::now(),
    })
}

impl Protection {
    pub fn reason(&self, remote: &str, reference: &str) -> Option<&'static str> {
        let short = reference.strip_prefix("refs/heads/").unwrap_or(reference);
        let remote_short = format!("{remote}/{short}");
        let overlaps = |tree: &Path| {
            self.claim_paths
                .iter()
                .any(|c| tree.starts_with(c) || c.starts_with(tree))
        };
        if Guards::protected_name(Path::new(reference)) {
            Some("protected-name")
        } else if self
            .claims
            .iter()
            .any(|b| b == short || b == reference || *b == remote_short)
        {
            Some("claimed")
        } else if self
            .trees
            .iter()
            .any(|(path, branch)| branch == reference && overlaps(path))
        {
            Some("claimed-worktree")
        } else if self.trees.iter().any(|(_, branch)| branch == reference) {
            Some("checked-out")
        } else if self.holds.contains(reference) || self.holds.contains(short) {
            Some("held-for-settlement")
        } else {
            None
        }
    }
}
