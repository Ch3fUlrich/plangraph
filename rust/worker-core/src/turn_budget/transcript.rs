use super::BudgetStatus;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
};

const CHUNK_MAX: u64 = 8 * 1024 * 1024;

pub fn message_key(id: &str) -> String {
    // Tagged hashes make migration from the original raw-ID array idempotent.
    if id.len() == 67 && id.starts_with("id:") && id[3..].bytes().all(|c| c.is_ascii_hexdigit()) {
        id.into()
    } else {
        format!("id:{:x}", Sha256::digest(id.as_bytes()))
    }
}

/// Consume complete JSONL records incrementally. API message IDs deduplicate
/// streamed content blocks, multiple tools and repeated hook deliveries.
pub fn ingest(payload: &Value, status: &mut BudgetStatus, now: u64) -> std::io::Result<()> {
    let Some(path) = payload["transcript_path"].as_str() else {
        return Ok(());
    };
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(e),
    };
    let size = file.metadata()?.len();
    if status.transcript_path != path || size < status.offset {
        status.offset = 0;
        status.transcript_path = path.into();
    }
    file.seek(SeekFrom::Start(status.offset))?;
    let mut bytes = Vec::new();
    file.take(CHUNK_MAX + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > CHUNK_MAX {
        return Err(std::io::Error::other("transcript delta too large"));
    }
    let Some(end) = bytes.iter().rposition(|&b| b == b'\n') else {
        return Ok(());
    };
    for line in bytes[..end].split(|&b| b == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if record["type"] != "assistant" || record["isSidechain"] == true {
            continue;
        }
        let Some(id) = record["message"]["id"].as_str().filter(|s| !s.is_empty()) else {
            continue;
        };
        let key = message_key(id);
        if status.seen.contains(&key) {
            continue;
        }
        status.seen.push_back(key);
        if status.seen.len() > 2048 {
            status.seen.pop_front();
        }
        let timestamp = record["timestamp"]
            .as_str()
            .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
            .and_then(|t| u64::try_from(t.timestamp()).ok())
            .unwrap_or(now);
        status.wake_calls = status.wake_calls.saturating_add(1);
        if timestamp <= now && now - timestamp < 3600 {
            status.calls.push(timestamp);
            if status.calls.len() > 8192 {
                status.calls.remove(0);
            }
        }
    }
    status.offset += end as u64 + 1;
    Ok(())
}
