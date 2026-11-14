//! Read shared records without rewriting them. Python's watchdog/bad-records.json
//! diagnostic output is intentionally not ported; malformed rows are skipped.
use crate::errors::ToolError;
use pg_worker_core::run::record::{run_dir, valid_run_id};
use serde_json::{json, Value};
use std::io::Read;
use std::path::Path;

const MAX_RECORD_BYTES: u64 = 8 * 1024 * 1024;

pub(crate) fn valid_item(value: &str) -> bool {
    let Some((project, id)) = value.split_once(':') else {
        return false;
    };
    value.len() <= 200
        && !project.is_empty()
        && !id.is_empty()
        && id.len() <= 180
        && project
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

pub(crate) fn annotate_item(base: &mut Value, card: &Value, events: &mut Vec<Value>) {
    let item = [&card["item_key"], &card["item"]]
        .into_iter()
        .find(|v| !v.is_null() && v.as_str() != Some(""))
        .cloned()
        .or_else(|| {
            std::env::var("PLANGRAPH_SPAWN_ITEM")
                .ok()
                .filter(|s| !s.is_empty())
                .map(Value::String)
        });
    base["item_key"] = match item {
        Some(Value::String(s)) if valid_item(&s) => json!(s),
        Some(_) => {
            if base.get("warnings").is_none() {
                base["warnings"] = json!([]);
            }
            base["warnings"]
                .as_array_mut()
                .unwrap()
                .push(json!("invalid item_key; stored as null"));
            Value::Null
        }
        None => Value::Null,
    };
    let missing = json!({"event":"seat_item_missing","level":"warning"});
    if base["item_key"].is_null()
        && matches!(base["role"].as_str(), Some("review" | "judge"))
        && !events.contains(&missing)
    {
        events.push(missing);
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn record_item_key_shape() {
        for key in [
            "plangraph:D-5047".to_owned(),
            "plan.graph-1_test:D-5047.part_2:review".to_owned(),
            format!("p:{}", "x".repeat(180)),
            format!("{}:{}", "p".repeat(19), "x".repeat(180)),
        ] {
            assert!(valid_item(&key), "{key}");
        }
        for key in [
            "garbage".to_owned(),
            ":x".to_owned(),
            "plangraph:".to_owned(),
            "".to_owned(),
            "bad/item".to_owned(),
            "plangraph:D-5047\n".to_owned(),
            "plangraph:D 5047".to_owned(),
            "plängraph:D-5047".to_owned(),
            "plangraph:D-５０４７".to_owned(),
            format!("p:{}", "x".repeat(181)),
            format!("{}:{}", "p".repeat(20), "x".repeat(180)),
        ] {
            assert!(!valid_item(&key), "{key}");
        }
    }

    #[test]
    fn guarded_open_rejects_fifo_without_blocking() {
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("record.json");
        let name = std::ffi::CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let (send, receive) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = send.send(open_record(&path).is_err());
        });
        let result = receive.recv_timeout(std::time::Duration::from_secs(2));
        std::fs::remove_dir_all(dir).unwrap();
        assert!(result.unwrap());
    }

    #[test]
    fn guarded_open_rejects_symlink_and_accepts_regular() {
        let dir = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("record.json");
        std::fs::write(&path, b"{}").unwrap();
        assert!(open_record(&path).is_ok());
        let link = dir.join("link.json");
        std::os::unix::fs::symlink(path, &link).unwrap();
        let rejected = open_record(&link).is_err();
        std::fs::remove_dir_all(dir).unwrap();
        assert!(rejected);
    }
}

fn open_record(path: &Path) -> std::io::Result<std::fs::File> {
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x00200000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(std::io::Error::other("invalid run record"));
        }
    }
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::other("invalid run record"));
    }
    Ok(file)
}

fn read_record_file(path: &Path) -> Result<Value, ToolError> {
    let invalid = || ToolError::Value("invalid run record".into());
    let read = || -> std::io::Result<Vec<u8>> {
        let metadata = std::fs::symlink_metadata(path)?;
        if !metadata.is_file() || metadata.len() > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("invalid run record"));
        }
        let file = open_record(path)?;
        let mut bytes = Vec::new();
        file.take(MAX_RECORD_BYTES + 1).read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    let bytes = read().map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            ToolError::Value("unknown run id".into())
        } else {
            invalid()
        }
    })?;
    if bytes.len() as u64 > MAX_RECORD_BYTES {
        return Err(invalid());
    }
    let mut record = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
    crate::record_status::reconcile(&mut record, path.parent().unwrap());
    Ok(record)
}

pub(crate) fn read_record(state: &Path, run_id: &Value) -> Result<Value, ToolError> {
    let id = run_id
        .as_str()
        .filter(|id| valid_run_id(id))
        .ok_or_else(|| ToolError::Value("unknown run id".into()))?;
    let path = run_dir(state, id).map_err(|_| ToolError::Value("unknown run id".into()))?;
    read_record_file(&path.join("record.json"))
}

pub(crate) fn list_runs(state: &Path) -> Vec<Value> {
    let mut paths: Vec<_> = std::fs::read_dir(state.join("runs"))
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|e| e.file_name().to_string_lossy().starts_with("run-"))
        .map(|e| e.path().join("record.json"))
        .collect();
    paths.sort();
    let mut runs = Vec::new();
    for path in paths {
        let Ok(mut record) = read_record_file(&path) else {
            continue;
        };
        if !record.is_object() || !record["run_id"].is_string() {
            continue;
        }
        for (source, dest) in [("parent_session", "parent_session"), ("item", "item_key")] {
            if (dest == "item_key" && record[dest].is_null())
                || (dest != "item_key" && record.get(dest).is_none())
            {
                if let Some(value) = record["completion"].get(source).cloned() {
                    if dest != "item_key" || value.as_str().is_some_and(valid_item) {
                        record[dest] = value;
                    }
                }
            }
        }
        if path.parent().unwrap().join("parent.delivered").exists() {
            record["_completion_acknowledged"] = true.into();
        }
        runs.push(record);
    }
    runs.sort_by(|a, b| {
        b["started"]
            .as_f64()
            .unwrap_or(0.0)
            .total_cmp(&a["started"].as_f64().unwrap_or(0.0))
    });
    runs
}
