use crate::fleet_name::string;
use crate::handoff::re;
use serde_json::{json, Value};
pub fn load(path: &std::path::Path) -> Vec<Value> {
    let text = std::fs::read_to_string(path).unwrap_or_default();
    let mut out = vec![];
    for raw in text.lines() {
        let line = re(r"\s#")
            .split(raw)
            .next()
            .unwrap_or("")
            .trim_end()
            .to_owned();
        if let Some(c) = re(r"^\s*-\s+id:\s*(\S+)$").captures(&line) {
            out.push(json!({"id":c[1].trim_matches(['\'', '"'])}));
        } else if let (Some(c), Some(cur)) =
            (re(r"^\s+(\w+):\s*(.*)$").captures(&line), out.last_mut())
        {
            let val = c[2].trim();
            let v = if val.starts_with('[') && val.ends_with(']') {
                json!(val[1..val.len() - 1]
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(|s| s.trim_matches(['\'', '"']))
                    .collect::<Vec<_>>())
            } else {
                json!(val.trim_matches(['\'', '"']))
            };
            cur[&c[1]] = v;
        }
    }
    out
}
pub fn of_path(path: &str, projects: &[Value]) -> Option<String> {
    for p in projects {
        let base = string(p, "path");
        if !std::path::Path::new(base).is_absolute() {
            continue;
        }
        for root in [
            base.to_owned(),
            format!("{base}-worktrees"),
            format!("{base}-lanes"),
            string(p, "worktrees").to_owned(),
        ] {
            if !root.is_empty()
                && (path == root
                    || path.starts_with(&(root.trim_end_matches('/').to_owned() + "/"))
                    || path.starts_with(&(root.trim_end_matches('\\').to_owned() + "\\")))
            {
                return Some(string(p, "id").to_owned());
            }
        }
    }
    None
}
