//! Locate the quota reader's SQLite source on the gateway host.
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;
use std::{
    env,
    ffi::OsString,
    io,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

const DATABASE: &str = "storage.sqlite";
const CONTAINER_STORE: &str = "/app/data";

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Mount {
    source: PathBuf,
    destination: String,
}

fn docker_store(container: OsString) -> io::Result<PathBuf> {
    let mut inspect = Command::new("docker");
    inspect
        .args([
            "inspect",
            "--type=container",
            "--format={{json .Mounts}}",
            "--",
        ])
        .arg(container);
    let stdout = pg_worker_core::host_command::output(&mut inspect, Duration::from_secs(10))
        .map_err(|_| {
            io::Error::other(format!(
                "cannot inspect the gateway {CONTAINER_STORE} mount"
            ))
        })?;
    let mounts: Vec<Mount> = serde_json::from_slice(&stdout).map_err(io::Error::other)?;
    let mut data = mounts
        .into_iter()
        .filter(|mount| mount.destination == CONTAINER_STORE);
    match (data.next(), data.next()) {
        (Some(mount), None) if mount.source.is_absolute() => Ok(mount.source.join(DATABASE)),
        _ => Err(io::Error::other(format!(
            "gateway has no unique host mount for {CONTAINER_STORE}"
        ))),
    }
}

fn non_empty(key: &str) -> Option<OsString> {
    env::var_os(key).filter(|value| !value.is_empty())
}

fn candidates(home: &Path) -> io::Result<Vec<PathBuf>> {
    if let Some(path) = non_empty("QUOTA_SNAPSHOT_DB") {
        return Ok(vec![path.into()]);
    }
    if let Some(path) = non_empty("OMNIROUTE_DATA_DIR") {
        return Ok(vec![PathBuf::from(path).join(DATABASE)]);
    }
    if let Some(container) = non_empty("QUOTA_GATEWAY_CONTAINER") {
        return Ok(vec![docker_store(container)?]);
    }
    let data_home = non_empty("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local/share"));
    Ok(vec![
        home.join(".omniroute").join(DATABASE),
        data_home.join("omniroute").join(DATABASE),
    ])
}

fn readable(path: &Path) -> bool {
    path.is_file()
        && Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .is_ok_and(|db| {
                db.prepare("SELECT id, provider, connection_id, window_key, remaining_percentage, is_exhausted, next_reset_at, created_at FROM quota_snapshots LIMIT 0")
                    .is_ok()
            })
}

pub(super) fn install(home: &Path, env_file: &Path) -> io::Result<()> {
    if super::exists(env_file)? {
        println!("SAME {}", env_file.display());
        return Ok(());
    }
    let searched = candidates(home)?;
    let selected = searched.iter().find(|path| readable(path)).ok_or_else(|| {
        let paths: Vec<_> = searched
            .iter()
            .map(|path| path.display().to_string())
            .collect();
        io::Error::other(format!(
            "no readable quota snapshot database; searched {}",
            paths.join(", ")
        ))
    })?;
    let value = super::assignment("QUOTA_SNAPSHOT_DB", &std::path::absolute(selected)?)?;
    super::create(env_file, value.as_bytes())
}
