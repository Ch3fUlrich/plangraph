pub mod fleet_name;
pub mod handoff;
pub mod path_check;
pub mod process;
pub mod question_count;

/// The private data checkout remains the source of operational data during cutover.
pub fn data_home() -> std::path::PathBuf {
    std::env::var_os("RESUME_DIGEST_HOME")
        .filter(|s| !s.is_empty())
        .map_or_else(|| home().join("code/plangraph-data/router"), Into::into)
}

pub fn read_json(path: &std::path::Path, limit: u64) -> Option<serde_json::Value> {
    if paths::secret(path) || std::fs::metadata(path).ok()?.len() > limit {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}
pub mod paths;

/// Python json.dumps' default separators, without escaping Unicode.
pub fn json_python(v: &serde_json::Value) -> String {
    use serde_json::Value;
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(json_python).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", serde_json::to_string(k).unwrap(), json_python(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => v.to_string(),
    }
}

pub fn home() -> std::path::PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(Into::into)
        .unwrap_or_else(|| ".".into())
}
