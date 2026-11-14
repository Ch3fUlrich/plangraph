pub mod backfill;
pub mod backfill_apply;
pub mod backfill_cli;
pub mod carried;
pub mod cli;
pub mod digest;
pub mod digest_card;
pub mod digest_git;
pub mod digest_keeper;
pub mod digest_sources;
pub mod digest_text;
pub mod executor;
pub mod fleet_name;
pub mod go_gate;
pub mod handoff;
pub mod keeper_wrapper;
pub mod merge_part;
pub mod merge_paths;
pub mod path_check;
pub mod process;
pub mod projects;
pub mod question_count;
pub mod summary;
pub mod summary_cli;
pub mod summary_sources;

pub fn python_lines(text: &str) -> Vec<String> {
    let mut chars = text.chars().peekable();
    let mut line = String::new();
    let mut out = vec![];
    while let Some(c) = chars.next() {
        if [
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ]
        .contains(&c)
        {
            out.push(std::mem::take(&mut line));
            if c == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
        } else {
            line.push(c);
        }
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

pub fn io_reason(e: &std::io::Error) -> String {
    use std::io::ErrorKind;
    match e.kind() {
        ErrorKind::NotFound => "No such file or directory".into(),
        ErrorKind::PermissionDenied => "Permission denied".into(),
        ErrorKind::AlreadyExists => "File exists".into(),
        ErrorKind::IsADirectory => "Is a directory".into(),
        ErrorKind::NotADirectory => "Not a directory".into(),
        _ => {
            let text = e.to_string();
            text.split(" (os error ").next().unwrap_or(&text).into()
        }
    }
}

/// Operational data can be selected explicitly during cutover.
pub fn data_home() -> std::path::PathBuf {
    std::env::var_os("RESUME_DIGEST_HOME")
        .filter(|s| !s.is_empty())
        .map_or_else(|| home().join(".local/share/plangraph/fleet"), Into::into)
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
