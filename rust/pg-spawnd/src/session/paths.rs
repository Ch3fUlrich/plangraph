use std::{
    env, fs, io,
    path::{Component, Path, PathBuf},
};

pub(super) struct Paths {
    pub state: PathBuf,
    pub home: PathBuf,
    pub ops: PathBuf,
}

impl Paths {
    pub fn new(ops: &Path) -> Self {
        let home = env::home_dir().unwrap_or_default();
        let state = var("ROUTING_STATE_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".local/state/routing"));
        Self {
            state,
            home,
            ops: ops.to_owned(),
        }
    }

    pub fn mcp(&self) -> PathBuf {
        match var("FLEET_MCP_CONFIG") {
            Some(path) if path.starts_with("~/") => self.home.join(&path[2..]),
            Some(path) if path == "~" => self.home.clone(),
            Some(path) => PathBuf::from(path),
            None => self.ops.join("mcp-fleet-abs.json"),
        }
    }

    pub fn remote_router(&self) -> Result<String, &'static str> {
        let configured = var("PLANGRAPH_FLEET_REMOTE_ROUTER");
        let router = resolve(&self.ops);
        let home = resolve(&self.home);
        let inferred = router.parent().and_then(|p| p.strip_prefix(home).ok());
        let relative = configured
            .as_deref()
            .map(Path::new)
            .or(inferred)
            .filter(|path| {
                path.components()
                    .all(|part| matches!(part, Component::Normal(_)))
            })
            .and_then(Path::to_str)
            .filter(|path| !path.is_empty());
        relative.map(str::to_owned).ok_or(
            "set PLANGRAPH_FLEET_REMOTE_ROUTER to the router directory relative to the destination home",
        )
    }
}

pub(super) fn var(key: &str) -> Option<String> {
    env::var(key).ok().filter(|s| !s.is_empty())
}

pub(super) fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        env::current_dir().unwrap_or_default().join(path)
    };
    resolve_absolute(&absolute, 40)
}

fn resolve_absolute(path: &Path, links: usize) -> PathBuf {
    let mut resolved = PathBuf::new();
    for part in path.components() {
        match part {
            Component::ParentDir => {
                resolved.pop();
            }
            Component::CurDir => {}
            _ => {
                resolved.push(part);
                if let Ok(canonical) = fs::canonicalize(&resolved) {
                    resolved = canonical;
                } else if links > 0 {
                    // A dangling symlink can still point outside a configured ledger root.
                    if let Ok(target) = fs::read_link(&resolved) {
                        resolved.pop();
                        resolved = resolve_absolute(&resolved.join(target), links - 1);
                    }
                }
            }
        }
    }
    resolved
}

pub(super) fn basename(name: &str) -> bool {
    !name.is_empty() && !name.contains('/') && name != "."
}

pub(super) fn read_text(path: &Path) -> io::Result<String> {
    Ok(normalize(&fs::read_to_string(path)?))
}

pub(super) fn normalize(text: &str) -> String {
    text.replace("\r\n", "\n").replace('\r', "\n")
}

pub(super) fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = quote.to_string();
    for ch in text.chars() {
        match ch {
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            '\\' => result.push_str("\\\\"),
            ch if ch == quote => {
                result.push('\\');
                result.push(ch);
            }
            ch if ch.is_control() && u32::from(ch) <= 255 => {
                result.push_str(&format!("\\x{:02x}", u32::from(ch)));
            }
            ch => result.push(ch),
        }
    }
    result.push(quote);
    result
}

pub(super) fn io_text(error: &io::Error, path: &Path) -> String {
    let Some(code) = error.raw_os_error() else {
        return error.to_string();
    };
    let raw = error.to_string();
    let description = raw.split(" (os error ").next().unwrap_or(&raw);
    format!(
        "[Errno {code}] {description}: {}",
        repr(&path.to_string_lossy())
    )
}

pub(super) fn shell_quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&ch))
    {
        return text.to_owned();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}
