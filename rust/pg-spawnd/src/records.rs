//! Read shared records; refresh native overflow through its Python owner.
//! Python watchdog/bad-records.json diagnostics remain unported; malformed rows are skipped.
use crate::errors::ToolError;
use pg_worker_core::run::record::{run_dir, valid_run_id};
use serde_json::{json, Value};
use std::path::Path;

pub(crate) fn valid_item(value: &str) -> bool {
    pg_spawnd::lane_guard::valid_key(value)
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

pub(crate) use pg_spawnd::record_file::open_record;

pub(crate) fn read_record_file(path: &Path) -> Result<Value, ToolError> {
    use pg_spawnd::record_file::{read_json, ReadError};
    read_json(path).map_err(|error| match error {
        ReadError::Missing => ToolError::Value("unknown run id".into()),
        ReadError::Invalid => ToolError::Value("invalid run record".into()),
    })
}

pub(crate) fn read_record(state: &Path, run_id: &Value) -> Result<Value, ToolError> {
    let id = run_id
        .as_str()
        .filter(|id| valid_run_id(id))
        .ok_or_else(|| ToolError::Value("unknown run id".into()))?;
    let path = run_dir(state, id).map_err(|_| ToolError::Value("unknown run id".into()))?;
    let record = read_record_file(&path.join("record.json"))?;
    if record["run_kind"] == "native-codex"
        && record["overflow_remote"] == true
        && record["status"] == "running"
    {
        return crate::placement::native_control(state, &record, "result");
    }
    Ok(record)
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
