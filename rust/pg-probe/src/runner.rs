//! Bounded runs through an explicitly pinned leg adapter; gold is never sent to it.
use crate::{Probe, RunConfig, RunRecord, Score, SourceStatus};
use pg_worker_core::stall::{supervise, Opts};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pinned {
    pub config: RunConfig,
    pub adapter: PathBuf,
    pub adapter_sha256: String,
    pub args: Vec<String>,
    pub timeout_seconds: u64,
    pub concurrency: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Observation {
    pub probe_id: String,
    pub response: RunRecord,
    pub score: Score,
    pub adapter_sha256: String,
}
fn invalid() -> io::Error {
    io::Error::other("probe_config_or_adapter_invalid")
}
pub fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
impl Pinned {
    pub fn validate(&self) -> io::Result<()> {
        let config = &self.config;
        if !(1..=4).contains(&self.concurrency)
            || !(1..=3600).contains(&self.timeout_seconds)
            || !config.fresh_session
            || !config.no_memory_dir
            || config.leg.trim().is_empty()
            || config.tool_set.is_empty()
            || config.tool_set.iter().any(|s| s.trim().is_empty())
            || config.repo_sha.len() != 40
            || !config
                .repo_sha
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            || !self.adapter.is_absolute()
            || !self.adapter.is_file()
            || self.args.iter().map(|s| s.len() + 1).sum::<usize>() > 32768
            || self.args.iter().any(|s| s.contains('\0'))
            || digest(&fs::read(&self.adapter)?) != self.adapter_sha256
        {
            return Err(invalid());
        }
        Ok(())
    }
}
fn run_one(
    probe: &Probe,
    sources: &BTreeMap<String, SourceStatus>,
    pinned: &Pinned,
    state: &Path,
) -> io::Result<Observation> {
    pinned.validate()?;
    let session = tempfile::Builder::new()
        .prefix("context-probe-")
        .tempdir_in(state)?;
    let request = session.path().join("request.json");
    let response = session.path().join("response.json");
    fs::write(
        &request,
        serde_json::to_vec(&serde_json::json!({
            "probe_id":probe.id, "question":probe.question, "config":pinned.config
        }))?,
    )?;
    let mut command = Command::new(&pinned.adapter);
    command
        .args(&pinned.args)
        .current_dir(session.path())
        .env_clear()
        .env("HOME", session.path())
        .env("USERPROFILE", session.path())
        .env("XDG_CONFIG_HOME", session.path())
        .env("XDG_DATA_HOME", session.path())
        .env("XDG_STATE_HOME", session.path())
        .env("XDG_CACHE_HOME", session.path())
        .env("TMPDIR", session.path())
        .env("TEMP", session.path())
        .env("TMP", session.path())
        .env("PG_PROBE_REQUEST_FILE", &request)
        .env("PG_PROBE_RESPONSE_FILE", &response);
    // PATH and Windows loader roots are the only inherited platform plumbing.
    for name in ["PATH", "SystemRoot", "WINDIR"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    let result = supervise(
        command,
        &Opts {
            timeout: Some(Duration::from_secs(pinned.timeout_seconds)),
            grace: Duration::from_secs(1),
            max_output: 4096,
            ..Opts::default()
        },
    )?;
    if result.code != Some(0)
        || result.timed_out
        || result.stalled
        || result.cancelled
        || fs::symlink_metadata(&response)?.file_type().is_symlink()
        || fs::metadata(&response)?.len() > 1024 * 1024
    {
        return Err(io::Error::other("probe_adapter_failed"));
    }
    let response: RunRecord = serde_json::from_slice(&fs::read(response)?)?;
    Ok(Observation {
        probe_id: probe.id.clone(),
        score: crate::score(probe, &response, sources, &pinned.config),
        response,
        adapter_sha256: pinned.adapter_sha256.clone(),
    })
}
/// Stable fixture order, at most four isolated sessions, and no partial-success return.
pub fn run_batch(
    probes: &[Probe],
    sources: &BTreeMap<String, SourceStatus>,
    pinned: &Pinned,
    state: &Path,
) -> io::Result<Vec<Observation>> {
    pinned.validate()?;
    fs::create_dir_all(state)?;
    let mut rows = Vec::new();
    for chunk in probes.chunks(pinned.concurrency) {
        let batch = std::thread::scope(|scope| {
            let handles: Vec<_> = chunk
                .iter()
                .map(|probe| scope.spawn(move || run_one(probe, sources, pinned, state)))
                .collect();
            handles
                .into_iter()
                .map(|h| {
                    h.join()
                        .map_err(|_| io::Error::other("probe_worker_panicked"))?
                })
                .collect::<io::Result<Vec<_>>>()
        })?;
        rows.extend(batch);
    }
    Ok(rows)
}

/// Re-evaluate saved answers after gold/source corrections without launching sessions.
pub fn rescore(
    probes: &[Probe],
    sources: &BTreeMap<String, SourceStatus>,
    config: &RunConfig,
    observations: &[Observation],
) -> io::Result<Vec<Observation>> {
    let saved: BTreeMap<_, _> = observations
        .iter()
        .map(|row| (row.probe_id.as_str(), row))
        .collect();
    if saved.len() != observations.len() || probes.len() != saved.len() {
        return Err(invalid());
    }
    probes
        .iter()
        .map(|probe| {
            let old = saved.get(probe.id.as_str()).ok_or_else(invalid)?;
            let mut row = (*old).clone();
            row.score = crate::score(probe, &row.response, sources, config);
            Ok(row)
        })
        .collect()
}
