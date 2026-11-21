//! Durable run-local queue used by the keeper CLI.
mod transport;
pub use transport::run;

use crate::atomic;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;
use std::fs::{self, File};
use std::io;
use std::path::{Path, PathBuf};

#[derive(Clone, Deserialize, Serialize)]
struct Entry {
    id: String,
    verb: String,
    body: Value,
    #[serde(default, skip_serializing_if = "is_false")]
    legacy: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    outcome: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    resolved_by: Option<String>,
}

fn is_false(value: &bool) -> bool {
    !*value
}

struct Queue {
    legacy: PathBuf,
    root: PathBuf,
    done: PathBuf,
}

impl Queue {
    fn open(legacy: &Path) -> io::Result<Self> {
        atomic::regular(legacy)?;
        let mut name = legacy.as_os_str().to_owned();
        name.push(".d");
        let root = PathBuf::from(name);
        let done = root.join("done");
        if let Some(parent) = root.parent().filter(|path| !path.as_os_str().is_empty()) {
            fs::create_dir_all(parent)?;
        }
        create_private_directory(&root)?;
        create_private_directory(&done)?;
        let queue = Self {
            legacy: legacy.to_owned(),
            root,
            done,
        };
        let _lock = atomic::lock(&queue.root.join("entries"))?;
        queue.import_legacy()?;
        Ok(queue)
    }

    fn bind_credential(&self, fingerprint: &str) -> io::Result<()> {
        let _entries = atomic::lock(&self.root.join("entries"))?;
        let path = self.root.join("credential");
        atomic::regular(&path)?;
        match fs::read_to_string(&path) {
            Ok(value) if value == fingerprint => Ok(()),
            Ok(_) => Err(io::Error::other("outbox belongs to another credential")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                atomic::write(&path, fingerprint.as_bytes())
            }
            Err(error) => Err(error),
        }
    }

    fn enqueue(&self, verb: &str, body: Value) -> io::Result<String> {
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).map_err(|error| io::Error::other(error.to_string()))?;
        let id = random
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let entry = Entry {
            id: id.clone(),
            verb: verb.to_owned(),
            body,
            legacy: false,
            outcome: None,
            resolved_by: None,
        };
        let _lock = atomic::lock(&self.root.join("entries"))?;
        self.write_next(&entry, self.next_sequence()?)?;
        Ok(id)
    }

    fn import_legacy(&self) -> io::Result<()> {
        use sha2::{Digest, Sha256};

        let text = match fs::read_to_string(&self.legacy) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        let mut ids = self
            .all_entries()?
            .into_iter()
            .map(|(_, entry)| entry.id)
            .collect::<HashSet<_>>();
        let mut seq = self.next_sequence()?;
        for (line_no, line) in text
            .lines()
            .enumerate()
            .filter(|(_, line)| !line.trim().is_empty())
        {
            let mut entry: Entry = serde_json::from_str(line).unwrap_or_else(|_| Entry {
                id: format!("{:x}", Sha256::digest(format!("{line_no}:{line}")))[..32].to_owned(),
                verb: "unreadable".into(),
                body: Value::Null,
                legacy: true,
                outcome: None,
                resolved_by: None,
            });
            entry.legacy = true;
            entry.outcome = None;
            entry.resolved_by = None;
            if ids.insert(entry.id.clone()) {
                self.write_next(&entry, seq)?;
                seq = seq
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("sequence exhausted"))?;
            }
        }
        Ok(())
    }

    fn next_sequence(&self) -> io::Result<u64> {
        files(&self.root)?
            .into_iter()
            .chain(files(&self.done)?)
            .map(|(seq, _)| seq)
            .max()
            .unwrap_or(0)
            .checked_add(1)
            .ok_or_else(|| io::Error::other("sequence exhausted"))
    }

    fn write_next(&self, entry: &Entry, seq: u64) -> io::Result<()> {
        atomic::write(
            &self.root.join(format!("{seq:020}.json")),
            &serde_json::to_vec(entry)?,
        )
    }

    fn drain(
        &self,
        until: Option<&str>,
        mut prepare: impl FnMut(&Entry) -> io::Result<Option<Value>>,
        mut send: impl FnMut(&Entry) -> io::Result<Value>,
    ) -> io::Result<Vec<Value>> {
        let _sender = atomic::lock(&self.root.join("sender"))?;
        let mut settled = read_entries(&self.done)?;
        if let Some(outcome) = settled.iter().find_map(|(_, entry)| {
            (Some(entry.id.as_str()) == until)
                .then(|| entry.outcome.clone())
                .flatten()
        }) {
            return Ok(vec![outcome]);
        }
        let mut results = Vec::new();
        loop {
            let Some((path, mut entry)) = self.first_pending()? else {
                return Ok(results);
            };
            if entry.verb == "submit" && entry.resolved_by.is_none() {
                if let Some((_, blocking)) = settled.iter().find(|(earlier_path, earlier)| {
                    earlier_path.file_name() < path.file_name()
                        && earlier.verb == "add_evidence"
                        && !evidence_complete(earlier)
                }) {
                    return Err(io::Error::other(format!(
                        "submit {} waits for evidence {}",
                        entry.id, blocking.id
                    )));
                }
            }
            if entry.outcome.is_none() {
                if entry.legacy && !entry.body.is_object() {
                    return Err(io::Error::other(format!(
                        "legacy entry {} requires recovery",
                        entry.id
                    )));
                }
                if !entry.legacy {
                    if let Some(bound_body) = prepare(&entry)? {
                        entry.body = bound_body;
                        atomic::write(&path, &serde_json::to_vec(&entry)?)?;
                    }
                }
                let outcome = send(&entry).map_err(|error| {
                    io::Error::other(format!("{} {}: {error}", entry.verb, entry.id))
                })?;
                if outcome["id"] != entry.id || !outcome["result"].is_string() {
                    return Err(io::Error::other("keeper did not return a matching receipt"));
                }
                if outcome["result"] == "recovery_required" {
                    return Err(io::Error::other(format!(
                        "legacy entry {} requires recovery",
                        entry.id
                    )));
                }
                entry.outcome = Some(outcome);
            }
            self.acknowledge(&path, &entry)?;
            results.push(entry.outcome.clone().unwrap());
            let stop = Some(entry.id.as_str()) == until;
            settled.push((path, entry));
            if stop {
                return Ok(results);
            }
        }
    }

    fn first_pending(&self) -> io::Result<Option<(PathBuf, Entry)>> {
        let _entries = atomic::lock(&self.root.join("entries"))?;
        files(&self.root)?
            .into_iter()
            .next()
            .map(|(_, path)| read_entry(&path).map(|entry| (path, entry)))
            .transpose()
    }

    fn all_entries(&self) -> io::Result<Vec<(PathBuf, Entry)>> {
        let mut entries = read_entries(&self.root)?;
        entries.extend(read_entries(&self.done)?);
        Ok(entries)
    }

    fn resolve(&self, id: &str, decision: &str) -> io::Result<()> {
        if !decision
            .strip_prefix("D-")
            .is_some_and(|rest| !rest.is_empty() && rest.bytes().all(|ch| ch.is_ascii_digit()))
        {
            return Err(io::Error::other("resolution requires a D-id"));
        }
        let _sender = atomic::lock(&self.root.join("sender"))?;
        let (path, mut entry) = self
            .all_entries()?
            .into_iter()
            .find(|(_, entry)| entry.id == id)
            .ok_or_else(|| io::Error::other("unknown invocation"))?;
        if !entry.legacy && (entry.verb != "add_evidence" || entry.outcome.is_none()) {
            return Err(io::Error::other(
                "only legacy obligations and refused evidence may be resolved",
            ));
        }
        if let Some(existing) = &entry.resolved_by {
            return if existing == decision {
                Ok(())
            } else {
                Err(io::Error::other("invocation already resolved"))
            };
        }
        entry.resolved_by = Some(decision.to_owned());
        if entry.outcome.is_none() {
            entry.outcome = Some(serde_json::json!({"id": id, "result": "resolved"}));
        }
        self.acknowledge(&path, &entry)
    }

    fn acknowledge(&self, path: &Path, entry: &Entry) -> io::Result<()> {
        let _entries = atomic::lock(&self.root.join("entries"))?;
        atomic::write(path, &serde_json::to_vec(entry)?)?;
        let target = self.done.join(path.file_name().unwrap());
        if path != target {
            fs::rename(path, &target)?;
            sync_directory(&self.done)?;
            sync_directory(&self.root)?;
        }
        Ok(())
    }
}

fn evidence_complete(entry: &Entry) -> bool {
    entry.resolved_by.is_some()
        || entry
            .outcome
            .as_ref()
            .is_some_and(|outcome| outcome["reply"]["verified"] == true)
}

fn files(root: &Path) -> io::Result<Vec<(u64, PathBuf)>> {
    let mut entries = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if let Some(seq) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.strip_suffix(".json"))
            .and_then(|name| name.parse().ok())
        {
            entries.push((seq, entry.path()));
        }
    }
    entries.sort_unstable();
    Ok(entries)
}

fn read_entry(path: &Path) -> io::Result<Entry> {
    atomic::regular(path)?;
    Ok(serde_json::from_reader(File::open(path)?)?)
}

fn read_entries(root: &Path) -> io::Result<Vec<(PathBuf, Entry)>> {
    files(root)?
        .into_iter()
        .map(|(_, path)| read_entry(&path).map(|entry| (path, entry)))
        .collect()
}

fn create_private_directory(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    match builder.create(path) {
        Ok(()) => sync_directory(
            path.parent()
                .filter(|p| !p.as_os_str().is_empty())
                .unwrap_or_else(|| Path::new(".")),
        ),
        Err(error)
            if error.kind() == io::ErrorKind::AlreadyExists
                && fs::symlink_metadata(path)?.file_type().is_dir() =>
        {
            Ok(())
        }
        Err(error) => Err(error),
    }
}

fn sync_directory(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    File::open(path)?.sync_all()?;
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

#[cfg(test)]
mod tests;
