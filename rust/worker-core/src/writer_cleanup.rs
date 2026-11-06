//! Local durable recording adapter. Only the trusted supervisor asserts group emptiness.
use crate::{
    hosts::RepoEntry,
    seat_pool::{common, git, lock},
    workspace_launch::verify_owned,
    writer_blob::{BlobKind, BlobStore, LocalBlobStore, SealedBlob},
    writer_result::{safe_id, WriterError},
};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
};
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LifecycleRecord {
    pub run_id: String,
    pub repo_id: String,
    pub checkout: PathBuf,
    pub failed_at_s: Option<i64>,
    pub group_empty: bool,
    pub result_blob: Option<SealedBlob>,
}
pub struct RecordedResult {
    state: PathBuf,
    run_id: String,
    bytes: Vec<u8>,
}
pub struct CleanupCandidate {
    pub run_id: String,
    pub checkout: PathBuf,
    pub eligible: bool,
}
fn directory(state: &Path) -> Result<PathBuf, WriterError> {
    let path = state.join("writer-lifecycle");
    LocalBlobStore::open(&path)?;
    Ok(path)
}
fn validate(record: &LifecycleRecord) -> Result<(), WriterError> {
    if !safe_id(&record.run_id)
        || !safe_id(&record.repo_id)
        || !record.checkout.is_absolute()
        || record.checkout.canonicalize()? != record.checkout
        || record.failed_at_s.is_some_and(|s| s < 0)
    {
        return Err(WriterError::Invalid);
    }
    Ok(())
}
fn write(path: &Path, name: &str, bytes: &[u8]) -> Result<(), WriterError> {
    let mut temp = tempfile::NamedTempFile::new_in(path)?;
    temp.write_all(bytes)?;
    temp.as_file().sync_all()?;
    temp.persist(path.join(name)).map_err(|_| WriterError::Io)?;
    #[cfg(unix)]
    fs::File::open(path)?.sync_all()?;
    Ok(())
}
fn read(path: &Path) -> Result<Vec<u8>, WriterError> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() || meta.len() > 16384 {
        return Err(WriterError::Verify);
    }
    Ok(fs::read(path)?)
}
pub(crate) fn load(state: &Path, run: &str) -> Result<LifecycleRecord, WriterError> {
    if !safe_id(run) {
        return Err(WriterError::Invalid);
    }
    let record: LifecycleRecord = serde_json::from_slice(&read(
        &state.join("writer-lifecycle").join(format!("{run}.json")),
    )?)
    .map_err(|_| WriterError::Verify)?;
    validate(&record)?;
    if record.run_id != run {
        return Err(WriterError::Verify);
    }
    Ok(record)
}
pub fn save_lifecycle(state: &Path, record: &LifecycleRecord) -> Result<(), WriterError> {
    validate(record)?;
    let dir = directory(state)?;
    let _guard = lock(&dir.join("metadata.lock"))?;
    write(
        &dir,
        &format!("{}.json", record.run_id),
        &serde_json::to_vec(record).map_err(|_| WriterError::Invalid)?,
    )
}
fn checked(state: &Path, record: &LifecycleRecord, blob: &SealedBlob) -> Result<(), WriterError> {
    let store = LocalBlobStore::open(&state.join("blobs"))?;
    let meta = store.metadata(&record.run_id)?;
    if meta.kind != BlobKind::Result
        || meta.repo_id != record.repo_id
        || meta.checkout != record.checkout
        || meta.blob != *blob
    {
        return Err(WriterError::Verify);
    }
    store.read(blob)?;
    Ok(())
}
#[rustfmt::skip]
pub fn record_result(state: &Path, run_id: &str, blob: &SealedBlob) -> Result<RecordedResult, WriterError> {
    let dir = directory(state)?;
    let _guard = lock(&dir.join("metadata.lock"))?;
    let mut record = load(state, run_id)?;
    checked(state, &record, blob)?;
    if record.result_blob.as_ref().is_some_and(|old| old != blob) {
        return Err(WriterError::Conflict);
    }
    record.result_blob = Some(blob.clone());
    let bytes = serde_json::to_vec(&record).map_err(|_| WriterError::Invalid)?;
    write(&dir, &format!("{run_id}.json"), &bytes)?;
    write(&dir, &format!("{run_id}.receipt"), &bytes)?;
    Ok(RecordedResult {
        state: state.canonicalize()?,
        run_id: run_id.into(),
        bytes,
    })
}
fn remove(repo: &RepoEntry, checkout: &Path) -> Result<(), WriterError> {
    if fs::symlink_metadata(checkout)?.file_type().is_symlink() {
        return Err(WriterError::Verify);
    }
    verify_owned(repo, checkout).map_err(|_| WriterError::Verify)?;
    let mut cmd = crate::workspace_resolver::git_command(Path::new(&repo.root));
    cmd.args([
        "-c",
        "core.hooksPath=",
        "worktree",
        "remove",
        "--force",
        "--",
    ]);
    let out = cmd.arg(checkout).output()?;
    if !out.status.success() {
        return Err(WriterError::Git);
    }
    Ok(())
}
#[rustfmt::skip]
pub fn remove_recorded_writer(repo: &RepoEntry, checkout: &Path, receipt: &RecordedResult) -> Result<(), WriterError> {
    let dir = directory(&receipt.state)?;
    let _guard = lock(&dir.join("metadata.lock"))?;
    if read(&dir.join(format!("{}.receipt", receipt.run_id)))? != receipt.bytes {
        return Err(WriterError::Unrecorded);
    }
    let record = load(&receipt.state, &receipt.run_id)?;
    if !record.group_empty || record.checkout != checkout {
        return Err(WriterError::Verify);
    }
    let blob = record.result_blob.as_ref().ok_or(WriterError::Unrecorded)?;
    let recorded: LifecycleRecord =
        serde_json::from_slice(&receipt.bytes).map_err(|_| WriterError::Verify)?;
    if recorded.repo_id != record.repo_id
        || recorded.checkout != checkout
        || recorded.result_blob.as_ref() != Some(blob)
    {
        return Err(WriterError::Verify);
    }
    checked(&receipt.state, &record, blob)?;
    remove(repo, checkout)
}
#[rustfmt::skip]
pub fn janitor(
    repo: &RepoEntry,
    state: &Path,
    now_s: i64,
    apply: bool,
) -> Result<Vec<CleanupCandidate>, WriterError> {
    let state = state.canonicalize()?;
    let state = state.as_path();
    let dir = state.join("writer-lifecycle");
    let mut candidates = Vec::new();
    if dir.try_exists()? {
        for entry in fs::read_dir(&dir)? {
            let path = entry?.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") { continue; }
            let Some(run) = path.file_stem().and_then(|s| s.to_str()) else { continue; };
            let Ok(record) = load(state, run) else { continue; };
            if verify_owned(repo, &record.checkout).is_err() { continue; }
            let receipt = read(&dir.join(format!("{run}.receipt"))).ok();
            let recorded = receipt.as_ref().is_some_and(|bytes| {
                serde_json::from_slice::<LifecycleRecord>(bytes).is_ok_and(|r| r.run_id == run && r.repo_id == record.repo_id && r.checkout == record.checkout && r.result_blob == record.result_blob)
            }) && record.result_blob.as_ref().is_some_and(|b| checked(state, &record, b).is_ok());
            let expired = |r: &LifecycleRecord| r.group_empty && r.failed_at_s.is_some_and(|s| now_s.saturating_sub(s) >= 86400);
            let eligible = record.group_empty && (record.failed_at_s.is_none() || expired(&record)) && (expired(&record) || recorded);
            if apply && eligible {
                if recorded {
                    let receipt = RecordedResult { state: state.into(), run_id: run.into(), bytes: receipt.ok_or(WriterError::Unrecorded)? };
                    remove_recorded_writer(repo, &record.checkout, &receipt)?;
                } else {
                    let _guard = lock(&dir.join("metadata.lock"))?;
                    let latest = load(state, run)?;
                    if !expired(&latest) || latest.checkout != record.checkout {
                        return Err(WriterError::Verify);
                    }
                    remove(repo, &latest.checkout)?;
                }
            }
            candidates.push(CleanupCandidate { run_id: run.into(), checkout: record.checkout, eligible });
        }
    }
    if apply {
        let common = common(repo)?;
        let _guard = lock(&common.join("spawner-prune.lock"))?;
        let stamp = common.join("spawner-prune-day");
        let day = now_s.div_euclid(86400).to_string();
        if fs::read_to_string(&stamp).ok().as_deref() != Some(&day) {
            git(Path::new(&repo.root), &["worktree", "prune"])?;
            write(&common, "spawner-prune-day", day.as_bytes())?;
        }
    }
    Ok(candidates)
}
#[cfg(test)]
#[path = "writer_cleanup_tests.rs"]
pub(crate) mod tests;
