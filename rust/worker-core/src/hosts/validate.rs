use super::*;

pub(super) fn validate_fields(value: &serde_json::Value) -> Result<(), HostsError> {
    fn check(
        value: &serde_json::Value,
        kind: &str,
        host: &str,
        repo: &str,
    ) -> Result<(), HostsError> {
        let allowed: &[&str] = match kind {
            "registry" => &["version", "self", "hosts"],
            "host" => &[
                "id",
                "class",
                "aliases",
                "transport",
                "os",
                "state_dir",
                "caps",
                "warmup",
                "session_mb",
                "roles",
                "repos",
            ],
            "caps" => &[
                "max_runs",
                "max_writers",
                "max_seats",
                "max_sessions",
                "mem_floor_mb",
            ],
            "warmup" => &["warmup_s", "ramp_mb"],
            "repo" => &[
                "root",
                "live_dir",
                "worktrees_root",
                "remote",
                "python",
                "import_roots",
                "cache_env",
            ],
            _ => unreachable!(),
        };
        if let Some(object) = value.as_object() {
            for key in object.keys() {
                if !allowed.contains(&key.as_str()) {
                    return Err(HostsError::UnknownField(v(
                        host,
                        repo,
                        key,
                        "unknown field",
                    )));
                }
            }
        }
        Ok(())
    }
    check(value, "registry", "-", "-")?;
    if let Some(hosts) = value["hosts"].as_array() {
        for host in hosts {
            let id = host["id"].as_str().unwrap_or("-");
            check(host, "host", id, "-")?;
            check(&host["caps"], "caps", id, "-")?;
            check(&host["warmup"], "warmup", id, "-")?;
            if let Some(repos) = host["repos"].as_object() {
                for (repo, entry) in repos {
                    check(entry, "repo", id, repo)?;
                }
            }
        }
    }
    Ok(())
}

// Inspect all string values and map keys, not just path fields. Only cache
// values may contain the token; requiring a whole segment keeps substitution safe.
pub(super) fn validate_slots(
    value: &serde_json::Value,
    host: &str,
    repo: &str,
    field: &str,
) -> Result<(), HostsError> {
    match value {
        serde_json::Value::String(s) if s.contains(SLOT) => {
            if field != "cache_env_value"
                || s.split(['/', '\\']).any(|p| p.contains(SLOT) && p != SLOT)
            {
                return Err(HostsError::SlotToken(v(host, repo, field, SLOT)));
            }
        }
        serde_json::Value::Array(items) => {
            for item in items {
                let id = item.get("id").and_then(|v| v.as_str()).unwrap_or(host);
                validate_slots(item, id, repo, field)?;
            }
        }
        serde_json::Value::Object(items) => {
            for (key, item) in items {
                if key.contains(SLOT) {
                    return Err(HostsError::SlotToken(v(host, repo, key, SLOT)));
                }
                let repo = if field == "repos" { key.as_str() } else { repo };
                let next = if field == "cache_env" {
                    "cache_env_value"
                } else {
                    key
                };
                validate_slots(item, host, repo, next)?;
            }
        }
        _ => {}
    }
    Ok(())
}

/// Violation message naming host id, repo id and field.
pub(super) fn v(host: &str, repo: &str, field: &str, msg: &str) -> String {
    format!("host={host} repo={repo} field={field}: {msg}")
}

/// Absolute by the rules of the HOST's `os`, never the running os (V3).
pub(super) fn abs_for(os: Os, p: &str) -> bool {
    let b = p.as_bytes();
    match os {
        Os::Linux => p.starts_with('/'),
        Os::Windows => {
            let drive = b.len() >= 3
                && b[0].is_ascii_alphabetic()
                && b[1] == b':'
                && (b[2] == b'\\' || b[2] == b'/');
            let tail = p
                .strip_prefix(r"\\")
                .or_else(|| p.strip_prefix("//"))
                .unwrap_or("");
            let mut it = tail.split(['\\', '/']);
            let unc = !it.next().unwrap_or("").is_empty() && !it.next().unwrap_or("").is_empty();
            drive || unc
        }
    }
}

/// Relative and free of `..` segments (V6).
fn rel_no_dotdot(os: Os, p: &str) -> bool {
    !abs_for(os, p)
        && !p.starts_with(['/', '\\'])
        && (p.len() < 2 || p.as_bytes()[1] != b':')
        && p.split(['/', '\\']).all(|s| s != "..")
}

pub(super) fn valid_id(id: &str) -> bool {
    id.len() <= 32
        && id.starts_with(|c: char| c.is_ascii_lowercase())
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

fn valid_repo_id(id: &str) -> bool {
    let first = id.chars().next().unwrap_or(' ');
    (first.is_ascii_lowercase() || first.is_ascii_digit())
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-'))
}

/// Lexical normalization (V4): `.`/`..` segments, repeated and trailing
/// separators; on windows hosts case-insensitive with `/` equal to `\`.
fn lex_norm(p: &str, os: Os) -> String {
    let (sep, s) = match os {
        Os::Linux => ('/', p.to_string()),
        Os::Windows => ('\\', p.replace('/', "\\").to_lowercase()),
    };
    let (prefix, rest) = match os {
        Os::Linux => ("/", s.as_str()),
        Os::Windows if s.len() >= 2 && s.as_bytes()[1] == b':' => {
            let cut = 3.min(s.len());
            (&s[..cut], &s[cut..])
        }
        Os::Windows if s.starts_with(r"\\") => {
            let mut it = s[2..].splitn(3, '\\');
            let (a, b) = (it.next().unwrap_or(""), it.next().unwrap_or(""));
            let n = (3 + a.len() + b.len()).min(s.len());
            (&s[..n], it.next().unwrap_or(""))
        }
        Os::Windows => ("", s.as_str()),
    };
    let mut segs: Vec<&str> = Vec::new();
    for seg in rest.split(sep) {
        if seg == ".." {
            segs.pop();
        } else if !seg.is_empty() && seg != "." {
            segs.push(seg);
        }
    }
    let joined = segs.join(&sep.to_string());
    if prefix.ends_with(sep) || joined.is_empty() {
        format!("{prefix}{joined}")
    } else {
        format!("{prefix}{sep}{joined}")
    }
}

/// Is `value` outside `base` after lexical normalization (V4)?
pub(super) fn outside(value: &str, base: &str, os: Os) -> bool {
    let sep = if os == Os::Windows { '\\' } else { '/' };
    let (nv, nb) = (lex_norm(value, os), lex_norm(base, os));
    let prefix = if nb.ends_with(sep) {
        nb.clone()
    } else {
        format!("{nb}{sep}")
    };
    !(nv == nb || nv.starts_with(&prefix))
}

/// Resolve symlinked ancestors even when the final path has not been created.
fn canonicalize_prefix(path: &str) -> Option<std::path::PathBuf> {
    let mut prefix = Path::new(path);
    let mut tail = Vec::new();
    loop {
        if let Ok(mut resolved) = std::fs::canonicalize(prefix) {
            for part in tail.iter().rev() {
                resolved.push(part);
            }
            return Some(resolved);
        }
        tail.push(prefix.file_name()?);
        prefix = prefix.parent()?;
    }
}

/// V3 and V5 for one path field; V4 out-of-tree when `repo` is given.
fn check_path(
    h: &HostEntry,
    repo_id: &str,
    field: &str,
    val: &str,
    is_self: bool,
    repo: Option<&RepoEntry>,
) -> Result<(), HostsError> {
    if field != "cache_env" && val.contains(SLOT) {
        return Err(HostsError::SlotToken(v(&h.id, repo_id, field, SLOT)));
    }
    if !abs_for(h.os, val) {
        return Err(HostsError::AbsolutePath(v(
            &h.id,
            repo_id,
            field,
            "not absolute for host os",
        )));
    }
    if let Some(repo) = repo {
        if !outside(val, &repo.worktrees_root, h.os) {
            return Err(HostsError::OutOfTree(v(
                &h.id,
                repo_id,
                field,
                "inside worktrees_root",
            )));
        }
        // For the `self` host also compare canonicalized forms (symlinks).
        if is_self {
            let canon = canonicalize_prefix(val).zip(canonicalize_prefix(&repo.worktrees_root));
            if let Some((cv, cb)) = canon {
                if !outside(&cv.to_string_lossy(), &cb.to_string_lossy(), h.os) {
                    return Err(HostsError::OutOfTree(v(
                        &h.id,
                        repo_id,
                        field,
                        "inside after canon",
                    )));
                }
            }
        }
    }
    Ok(())
}

pub(super) fn validate_host(h: &HostEntry, is_self: bool) -> Result<(), HostsError> {
    for (index, role) in h.roles.iter().enumerate() {
        if h.roles[..index].contains(role) {
            return Err(HostsError::DuplicateRole(v(
                &h.id,
                "-",
                "roles",
                "duplicate role",
            )));
        }
    }
    check_path(h, "-", "state_dir", &h.state_dir, is_self, None)?;
    for (repo_id, repo) in &h.repos {
        if !valid_repo_id(repo_id) {
            return Err(HostsError::NumbersShape(v(
                &h.id,
                repo_id,
                "repo_id",
                "bad repo id",
            )));
        }
        if repo_id.contains(SLOT) || repo.remote.contains(SLOT) {
            return Err(HostsError::SlotToken(v(
                &h.id,
                repo_id,
                "repo_id/remote",
                SLOT,
            )));
        }
        check_path(h, repo_id, "root", &repo.root, is_self, None)?;
        check_path(
            h,
            repo_id,
            "worktrees_root",
            &repo.worktrees_root,
            is_self,
            None,
        )?;
        if let Some(py) = &repo.python {
            check_path(h, repo_id, "python", py, is_self, Some(repo))?;
        }
        for imp in &repo.import_roots {
            if imp.contains(SLOT) {
                return Err(HostsError::SlotToken(v(
                    &h.id,
                    repo_id,
                    "import_roots",
                    SLOT,
                )));
            }
            if !rel_no_dotdot(h.os, imp) {
                return Err(HostsError::NumbersShape(v(
                    &h.id,
                    repo_id,
                    "import_roots",
                    "not relative",
                )));
            }
        }
        for (key, val) in &repo.cache_env {
            if key.contains(SLOT) {
                return Err(HostsError::SlotToken(v(&h.id, repo_id, "cache_env", SLOT)));
            }
            check_path(h, repo_id, "cache_env", val, is_self, Some(repo))?;
        }
    }
    // V6 numbers and ramp roles.
    let c = &h.caps;
    if h.class == HostClass::Restricted
        && (c.max_runs > 4
            || c.max_writers > 2
            || c.max_seats > 2
            || c.max_sessions > 5
            || c.mem_floor_mb < 6144)
    {
        return Err(HostsError::NumbersShape(v(
            &h.id,
            "-",
            "class/caps",
            "restricted requires caps <= 4/2/2/5 and mem_floor_mb >= 6144",
        )));
    }
    for (ok, field, msg) in [
        (c.max_runs >= 1, "max_runs", "must be >= 1"),
        (
            c.max_writers <= c.max_runs,
            "max_writers",
            "must be <= max_runs",
        ),
        (
            c.max_seats <= c.max_runs,
            "max_seats",
            "must be <= max_runs",
        ),
        (h.warmup.warmup_s > 0, "warmup_s", "must be > 0"),
        (h.session_mb > 0, "session_mb", "must be > 0"),
    ] {
        if !ok {
            return Err(HostsError::NumbersShape(v(&h.id, "-", field, msg)));
        }
    }
    for key in h.warmup.ramp_mb.keys() {
        if key != "session" && !h.roles.iter().any(|r| ROLE_KEYS[*r as usize] == key) {
            return Err(HostsError::NumbersShape(v(
                &h.id,
                "-",
                "ramp_mb",
                "role not in roles",
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
#[path = "validate_tests.rs"]
mod tests;
