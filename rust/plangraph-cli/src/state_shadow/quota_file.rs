//! The role's quota file: written each scan from the router's own quota states, read back as a source.
use super::{Env, Result};
use crate::feeder::quota::{live_leg_limits, live_snapshots, remaining_by_provider};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// A file older than this was not refreshed by a live writer.
pub(super) const MAX_AGE_SECONDS: u64 = 600;
const SNAPSHOT_MAX_AGE_SECONDS: i64 = 10_800;

pub(super) fn document(
    levels: Option<&BTreeMap<String, f64>>,
    limits: Option<&BTreeMap<String, i64>>,
) -> Value {
    let rounded = levels.map(|levels| {
        levels
            .iter()
            .map(|(provider, left)| (provider.clone(), left.round() as i64))
            .collect::<BTreeMap<_, _>>()
    });
    json!({"remaining_percent": rounded, "limited_until": limits})
}

pub(super) fn write(
    path: &Path,
    levels: Option<&BTreeMap<String, f64>>,
    limits: Option<&BTreeMap<String, i64>>,
) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(&document(levels, limits)).map_err(|_| "quota encode")?;
    pg_worker_core::run::record_io::atomic_write_private(path, &bytes)
        .map_err(|_| "cannot write quota status".into())
}

/// What the two sources gave. A source that could not be read is `None`, which the file records
/// as `null`; an empty healthy state is a value. Both unreadable fails: the file must then go
/// stale for the collector to report it, not be rewritten empty.
pub(super) fn combine<L, M>(
    levels: std::result::Result<L, String>,
    limits: std::result::Result<M, String>,
) -> Result<(Option<L>, Option<M>)> {
    match (levels, limits) {
        (Err(first), Err(second)) => Err(format!("quota sources unavailable: {first}; {second}")),
        (levels, limits) => Ok((levels.ok(), limits.ok())),
    }
}

/// Refresh `path` from the router's own states: the limits routing treats as active (AGY, Codex
/// and every other leg, through its scoped reader) and the remaining percent of every provider in
/// the quota snapshots.
pub(super) fn refresh(path: &Path, env: &Env, repo: &Path, now: i64) -> Result<()> {
    let levels = live_snapshots(env, repo)
        .map(|rows| remaining_by_provider(&rows, now, SNAPSHOT_MAX_AGE_SECONDS));
    let limits = live_leg_limits(env, repo);
    let (levels, limits) = combine(levels, limits)?;
    write(path, levels.as_ref(), limits.as_ref())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_shadow::{fixture, local};
    use std::process::Command;

    fn repo() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
    }

    /// Run routing's own recorder against the fixture state, as the router and probes do.
    fn record(env: &Env, code: &str) {
        let output = Command::new("python3")
            .current_dir(repo())
            .envs(env)
            .env("PYTHONPATH", repo().join("src"))
            .args([
                "-c",
                &format!(
                    "from plangraph.routing_support.leg_state import _default_store\ns = _default_store()\n{code}"
                ),
            ])
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
    }

    #[test]
    fn levels_are_written_as_whole_percent_per_provider_beside_the_limits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("quota.json");
        let levels = BTreeMap::from([("agy".to_owned(), 41.6), ("codex".to_owned(), 7.2)]);
        let limits = BTreeMap::from([("agy:x".to_owned(), 5)]);
        write(&path, Some(&levels), Some(&limits)).unwrap();
        let read: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            read,
            json!({"remaining_percent": {"agy": 42, "codex": 7}, "limited_until": {"agy:x": 5}})
        );
    }

    #[test]
    fn an_unreadable_source_is_null_and_two_unreadable_sources_fail_the_refresh() {
        let bad = || Err::<u8, String>("down".into());
        assert_eq!(combine(Ok(1u8), bad()).unwrap(), (Some(1), None));
        assert_eq!(combine(bad(), Ok(2u8)).unwrap(), (None, Some(2)));
        assert!(combine(bad(), bad()).unwrap_err().contains("unavailable"));
        // An empty healthy state is a value, not an unavailable source.
        assert_eq!(
            combine(Ok(BTreeMap::<String, f64>::new()), bad())
                .unwrap()
                .0,
            Some(BTreeMap::new())
        );
    }

    #[test]
    fn booking_and_recovery_of_an_agy_limit_reach_the_collector_through_refresh() {
        let dir = tempfile::tempdir().unwrap();
        let config = fixture(dir.path());
        let state = dir.path().join("xdg");
        let env: Env = BTreeMap::from([
            ("XDG_STATE_HOME".into(), state.display().to_string()),
            ("PLANGRAPH_OMNIROUTE_LEG_STATE".into(), "0".into()),
            (
                "QUOTA_SNAPSHOT_DB".into(),
                dir.path().join("none.sqlite").display().to_string(),
            ),
        ]);
        // The store exists once routing has recorded anything; an empty healthy state follows.
        record(&env, "s.record_probe_pass('codex-gpt', 'fixture')");
        let now = || chrono::Utc::now().timestamp();
        let read = |cursor: Value| {
            refresh(&config.quota, &env, &repo(), now()).unwrap();
            local::quota(&config, cursor).unwrap()
        };
        let (cursor, baseline, _) = read(Value::Null);
        assert_eq!(baseline.len(), 1);
        assert_eq!(baseline[0].data["limited_until"], json!({}));
        record(
            &env,
            "s.record_harness_limit('agy/gemini-4-hydrogen-high', 'RESOURCE_EXHAUSTED (code 429): Per-model quota reached. Resets in 10h40m26s.', 'fixture')",
        );
        let (cursor, booked, _) = read(cursor);
        assert_eq!(booked.len(), 1, "a booked limit is a quota event");
        assert!(booked[0].data["limited_until"]["agy:gemini-4-hydrogen"].is_i64());
        // Routing clears the limit after a successful probe; so must the shadow file.
        record(
            &env,
            "s.record_probe_pass('agy/gemini-4-hydrogen-high', 'fixture')",
        );
        let (_, recovered, _) = read(cursor);
        assert_eq!(recovered.len(), 1, "recovery is a quota event");
        assert_eq!(recovered[0].data["limited_until"], json!({}));
    }

    #[test]
    fn a_missing_store_and_unreadable_snapshots_leave_the_existing_file_untouched() {
        let dir = tempfile::tempdir().unwrap();
        let config = fixture(dir.path());
        std::fs::write(&config.quota, "{\"previous\":true}").unwrap();
        let env: Env = BTreeMap::from([
            (
                "XDG_STATE_HOME".into(),
                dir.path().join("xdg").display().to_string(),
            ),
            (
                "QUOTA_SNAPSHOT_DB".into(),
                dir.path().join("none.sqlite").display().to_string(),
            ),
        ]);
        let error = refresh(&config.quota, &env, &repo(), 1000).unwrap_err();
        assert!(error.contains("unavailable"), "{error}");
        assert_eq!(
            std::fs::read_to_string(&config.quota).unwrap(),
            "{\"previous\":true}"
        );
        // A zero-byte store is as unknown as a missing one.
        let store = dir.path().join("xdg/plangraph");
        std::fs::create_dir_all(&store).unwrap();
        std::fs::write(store.join("leg-state.sqlite"), "").unwrap();
        assert!(refresh(&config.quota, &env, &repo(), 1000).is_err());
    }

    #[test]
    fn a_busy_refusal_is_down_in_routing_and_stays_out_of_the_limits() {
        let dir = tempfile::tempdir().unwrap();
        let config = fixture(dir.path());
        let env: Env = BTreeMap::from([
            (
                "XDG_STATE_HOME".into(),
                dir.path().join("xdg").display().to_string(),
            ),
            (
                "QUOTA_SNAPSHOT_DB".into(),
                dir.path().join("none.sqlite").display().to_string(),
            ),
        ]);
        record(
            &env,
            "s.record_harness_limit('codex-gpt', 'HTTP 503: provider busy', 'spawner:harness', down_s=600)",
        );
        refresh(&config.quota, &env, &repo(), 1000).unwrap();
        let (_, events, _) = local::quota(&config, Value::Null).unwrap();
        assert_eq!(events[0].data["limited_until"], json!({}));
    }
}
