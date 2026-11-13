//! Env-file values are never included in diagnostics.
use std::collections::BTreeMap;
use std::path::Path;

pub fn parse(text: &str) -> Result<BTreeMap<String, String>, String> {
    let mut values = BTreeMap::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| format!("invalid env line {}", i + 1))?;
        if key.is_empty()
            || !key
                .bytes()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == b'_')
            || key.as_bytes()[0].is_ascii_digit()
        {
            return Err(format!("invalid env key on line {}", i + 1));
        }
        if !key.ends_with("_FILE")
            && ["TOKEN", "SECRET", "PASSWORD", "API_KEY"]
                .iter()
                .any(|s| key.contains(s))
        {
            return Err(format!("inline secret refused on line {}", i + 1));
        }
        let value = value.trim();
        let value = if value.starts_with(['\'', '"']) {
            if value.len() < 2 || value.as_bytes().last() != value.as_bytes().first() {
                return Err(format!("unclosed quote on line {}", i + 1));
            }
            &value[1..value.len() - 1]
        } else {
            value
        };
        if value.contains('\0') || values.insert(key.into(), value.into()).is_some() {
            return Err(format!("invalid or duplicate env line {}", i + 1));
        }
    }
    Ok(values)
}

pub fn load(
    path: &Path,
    process: impl IntoIterator<Item = (String, String)>,
) -> Result<BTreeMap<String, String>, String> {
    let mut values = BTreeMap::from([
        ("PG_URL".into(), "http://127.0.0.1:8765".into()),
        ("PG_LEASE_S".into(), "7200".into()),
        ("PG_HEARTBEAT_S".into(), "900".into()),
    ]);
    match std::fs::read_to_string(path) {
        Ok(text) => values.extend(parse(&text)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(_) => return Err("env file unreadable".into()),
    }
    values.extend(process);
    Ok(values)
}
