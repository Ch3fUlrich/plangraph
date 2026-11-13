pub mod handoff;
pub mod path_check;
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
