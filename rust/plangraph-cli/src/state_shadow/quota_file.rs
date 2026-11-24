//! The role's quota file: written from the live quota snapshots each scan, read back as a source.
use super::{Env, Result};
use crate::feeder::quota::{live_snapshots, remaining_by_provider};
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::Path;

/// A file older than this was not refreshed by a live writer.
pub(super) const MAX_AGE_SECONDS: u64 = 600;
const SNAPSHOT_MAX_AGE_SECONDS: i64 = 10_800;

/// Seconds a limit without a stated reset stays booked (routing's `REPROBE_MINUTES`).
const REPROBE_SECONDS: f64 = 600.0;

/// The provider limits routing has booked and not yet seen expire, by leg key, as the epoch second
/// each ends: the latest provider-response or harness-stderr event of a leg that is not a 200 and
/// whose reset (or reprobe window) is still ahead. This is the native AGY and Codex state the
/// router reads from `leg-state.sqlite`, not a copy through another gateway.
pub(super) fn active_limits(db: &Path, now: f64) -> Result<BTreeMap<String, i64>> {
    if !db.is_file() {
        return Ok(BTreeMap::new());
    }
    let conn = Connection::open_with_flags(db, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| "cannot open leg state")?;
    let mut rows = conn
        .prepare(
            "SELECT leg_key, status, reset_at, ts FROM leg_events \
             WHERE source IN ('harness-stderr','provider-response') AND ts <= ?1 \
             ORDER BY ts, id",
        )
        .map_err(|_| "cannot read leg state")?;
    let mut latest: BTreeMap<String, (i64, Option<f64>, f64)> = BTreeMap::new();
    let rows = rows
        .query_map([now], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, Option<f64>>(2)?,
                row.get::<_, f64>(3)?,
            ))
        })
        .map_err(|_| "cannot read leg state")?;
    for row in rows {
        let (key, status, reset, ts) = row.map_err(|_| "cannot read leg state")?;
        latest.insert(key, (status, reset, ts));
    }
    Ok(latest
        .into_iter()
        .filter(|(_, (status, _, _))| *status != 200)
        .map(|(key, (_, reset, ts))| (key, reset.unwrap_or(ts + REPROBE_SECONDS)))
        .filter(|(_, end)| *end > now)
        .map(|(key, end)| (key, end as i64))
        .collect())
}

pub(super) fn document(
    levels: Option<&BTreeMap<String, f64>>,
    limits: &BTreeMap<String, i64>,
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
    limits: &BTreeMap<String, i64>,
) -> Result<()> {
    let bytes = serde_json::to_vec_pretty(&document(levels, limits)).map_err(|_| "quota encode")?;
    pg_worker_core::run::record_io::atomic_write_private(path, &bytes)
        .map_err(|_| "cannot write quota status".into())
}

/// Refresh `path` from the router's own states: the booked leg limits (AGY, Codex) and the
/// remaining percent of every provider in the quota snapshots. A failed snapshot read leaves that
/// part `null` without hiding the limits; a failed limits read fails the refresh.
pub(super) fn refresh(
    path: &Path,
    env: &Env,
    repo: &Path,
    leg_state: &Path,
    now: i64,
) -> Result<()> {
    let limits = active_limits(leg_state, now as f64)?;
    let levels = live_snapshots(env, repo)
        .ok()
        .map(|rows| remaining_by_provider(&rows, now, SNAPSHOT_MAX_AGE_SECONDS));
    write(path, levels.as_ref(), &limits)
}

/// Where the router books provider limits: `$XDG_STATE_HOME/plangraph/leg-state.sqlite`.
pub(super) fn leg_state_path(env: &Env, home: &Path) -> std::path::PathBuf {
    env.get("XDG_STATE_HOME")
        .filter(|dir| !dir.is_empty())
        .map_or_else(|| home.join(".local/state"), std::path::PathBuf::from)
        .join("plangraph/leg-state.sqlite")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state_shadow::{fixture, local};

    fn book(db: &Path, rows: &[(&str, i64, Option<f64>, f64, &str)]) {
        let conn = Connection::open(db).unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS leg_events (id INTEGER PRIMARY KEY, leg_key TEXT, \
             account TEXT, combo TEXT, status INTEGER, body_excerpt TEXT, retry_after_s REAL, \
             reset_at REAL, ts REAL, caller TEXT, source TEXT)",
        )
        .unwrap();
        for (leg, status, reset, ts, source) in rows {
            conn.execute(
                "INSERT INTO leg_events (leg_key, status, reset_at, ts, source) VALUES (?1,?2,?3,?4,?5)",
                rusqlite::params![leg, status, reset, ts, source],
            )
            .unwrap();
        }
    }

    #[test]
    fn levels_are_written_as_whole_percent_per_provider_beside_the_limits() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("quota.json");
        let levels = BTreeMap::from([("agy".to_owned(), 41.6), ("codex".to_owned(), 7.2)]);
        write(
            &path,
            Some(&levels),
            &BTreeMap::from([("agy:x".to_owned(), 5)]),
        )
        .unwrap();
        let read: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(
            read,
            json!({"remaining_percent": {"agy": 42, "codex": 7}, "limited_until": {"agy:x": 5}})
        );
    }

    #[test]
    fn booked_agy_and_codex_limits_are_read_from_the_router_state() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("leg-state.sqlite");
        assert!(active_limits(&db, 1000.0).unwrap().is_empty());
        book(
            &db,
            &[
                (
                    "agy:gemini-4-hydrogen",
                    429,
                    Some(5000.0),
                    900.0,
                    "harness-stderr",
                ),
                ("codex-gpt", 429, None, 950.0, "provider-response"),
                (
                    "agy:gemini-4-argon",
                    429,
                    Some(990.0),
                    800.0,
                    "harness-stderr",
                ),
                (
                    "agy:gemini-4-neon",
                    429,
                    Some(9000.0),
                    700.0,
                    "harness-stderr",
                ),
                ("agy:gemini-4-neon", 200, None, 800.0, "provider-response"),
                ("agy:probe-only", 503, Some(9000.0), 700.0, "liveness-probe"),
            ],
        );
        assert_eq!(
            active_limits(&db, 1000.0).unwrap(),
            BTreeMap::from([
                ("agy:gemini-4-hydrogen".to_owned(), 5000),
                ("codex-gpt".to_owned(), 1550),
            ])
        );
    }

    #[test]
    fn a_change_of_the_router_quota_state_reaches_the_collector_as_a_quota_event() {
        let dir = tempfile::tempdir().unwrap();
        let config = fixture(dir.path());
        let db = dir.path().join("leg-state.sqlite");
        let refresh_from_state = |now: f64| {
            let limits = active_limits(&db, now).unwrap();
            write(&config.quota, None, &limits).unwrap();
        };
        refresh_from_state(1000.0);
        let (cursor, first, _) = local::quota(&config, Value::Null).unwrap();
        assert_eq!(first.len(), 1, "the first read is the baseline level");
        let (cursor, quiet, _) = local::quota(&config, cursor).unwrap();
        assert!(quiet.is_empty());
        book(
            &db,
            &[(
                "agy:gemini-4-hydrogen",
                429,
                Some(5000.0),
                900.0,
                "harness-stderr",
            )],
        );
        refresh_from_state(1000.0);
        let (cursor, limited, _) = local::quota(&config, cursor).unwrap();
        assert_eq!(limited.len(), 1);
        assert_eq!(
            limited[0].data["limited_until"]["agy:gemini-4-hydrogen"],
            5000
        );
        // The booked reset passes: the limit leaves the file and the collector reports it.
        refresh_from_state(6000.0);
        assert_eq!(local::quota(&config, cursor).unwrap().1.len(), 1);
    }
}
