use super::{Checkpoint, Event};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

#[derive(Deserialize, Serialize)]
struct Pending {
    offset: u64,
    events: String,
    digest: String,
    full: String,
    checkpoint: Checkpoint,
}

fn optional<T: for<'a> Deserialize<'a>>(file: &Path) -> io::Result<Option<T>> {
    match fs::read(file) {
        Ok(data) => serde_json::from_slice(&data)
            .map(Some)
            .map_err(io::Error::other),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn publish(directory: &Path, pending: &Pending) -> io::Result<()> {
    let mut options = OpenOptions::new();
    options.create(true).truncate(false).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut log = options.open(directory.join("events.jsonl"))?;
    let size = log.metadata()?.len();
    if !(pending.offset..=pending.checkpoint.bytes).contains(&size)
        || pending.offset + pending.events.len() as u64 != pending.checkpoint.bytes
    {
        return Err(io::Error::other(
            "event file disagrees with the pending scan",
        ));
    }
    log.seek(SeekFrom::Start(pending.offset))?;
    let mut prefix = Vec::new();
    log.read_to_end(&mut prefix)?;
    if !pending.events.as_bytes().starts_with(&prefix) {
        return Err(io::Error::other(
            "event file has a conflicting pending scan",
        ));
    }
    log.write_all(&pending.events.as_bytes()[prefix.len()..])?;
    log.sync_all()?;
    let scan = pending.checkpoint.scan;
    crate::atomic::write(
        &directory.join(format!("digest-{scan}.full.txt")),
        pending.full.as_bytes(),
    )?;
    crate::atomic::write(
        &directory.join(format!("digest-{scan}.txt")),
        pending.digest.as_bytes(),
    )?;
    crate::atomic::write(
        &directory.join("bookmark.json"),
        &serde_json::to_vec(&pending.checkpoint)?,
    )?;
    fs::remove_file(directory.join("pending.json"))?;
    Ok(())
}

pub(super) fn load(directory: &Path) -> io::Result<Checkpoint> {
    if let Some(pending) = optional::<Pending>(&directory.join("pending.json"))? {
        publish(directory, &pending)?;
    }
    Ok(optional(&directory.join("bookmark.json"))?.unwrap_or_default())
}

pub(super) fn commit(
    directory: &Path,
    offset: u64,
    next: &mut Checkpoint,
    events: &[Event],
) -> io::Result<()> {
    let mut rows = String::new();
    for event in events {
        rows.push_str(&serde_json::to_string(event)?);
        rows.push('\n');
    }
    next.bytes = offset
        .checked_add(rows.len() as u64)
        .ok_or_else(|| io::Error::other("event file offset exhausted"))?;
    let range = format!(
        "Events bytes {offset}..{} (scan {})\n",
        next.bytes, next.scan
    );
    let mut full = range.clone();
    for event in events
        .iter()
        .filter(|row| row.urgent)
        .chain(events.iter().filter(|row| !row.urgent))
    {
        full.push_str(&format!(
            "{}{} {}{} {}\n",
            if event.urgent { "URGENT " } else { "" },
            event.source,
            event.kind,
            if event.untrusted { " [untrusted]" } else { "" },
            event.data,
        ));
    }
    let digest = if full.chars().count() > 6000 {
        let footer = format!("\nFull digest in digest-{}.full.txt\n", next.scan);
        let mut excerpt: String = full.chars().take(6000 - footer.chars().count()).collect();
        excerpt.push_str(&footer);
        excerpt
    } else {
        full.clone()
    };
    let data = serde_json::json!({
        "offset":offset, "events": rows, "full":full, "digest":digest, "checkpoint": next,
    });
    let path = directory.join("pending.json");
    let bytes = serde_json::to_vec(&data)?;
    crate::atomic::write(&path, &bytes)?;
    publish(directory, &serde_json::from_slice(&bytes)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resumes_partial_append_before_advancing_bookmarks() {
        let dir = tempfile::tempdir().unwrap();
        let events = "{\"type\":\"fixture\"}\n".to_owned();
        let pending = Pending {
            offset: 0,
            checkpoint: Checkpoint {
                bytes: events.len() as u64,
                scan: 1,
                ..Checkpoint::default()
            },
            events: events.clone(),
            full: "full".into(),
            digest: "digest".into(),
        };
        fs::write(
            dir.path().join("pending.json"),
            serde_json::to_vec(&pending).unwrap(),
        )
        .unwrap();
        fs::write(dir.path().join("events.jsonl"), &events[..7]).unwrap();
        assert_eq!(load(dir.path()).unwrap().scan, 1);
        assert_eq!(load(dir.path()).unwrap().scan, 1);
        assert_eq!(
            fs::read_to_string(dir.path().join("events.jsonl")).unwrap(),
            events
        );
    }
}
