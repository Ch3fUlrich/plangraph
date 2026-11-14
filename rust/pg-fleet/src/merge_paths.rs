//! Wrapper lookup never searches the implicit working directory.
use std::path::{Path, PathBuf};
pub fn wrapper(kind: &str, override_: Option<&Path>, dirs: &[PathBuf]) -> Result<PathBuf, String> {
    let (name, label) = if kind == "pgk" {
        ("pgk.sh", "keeper")
    } else {
        ("ci_evidence_only.sh", "evidence")
    };
    if let Some(p) = override_.filter(|p| !p.as_os_str().is_empty() && p.is_file()) {
        return crate::paths::resolve(p).map_err(|_| format!("{label} wrapper unavailable"));
    }
    for d in dirs.iter().filter(|d| d.is_absolute()) {
        let p = d.join(name);
        if !p.is_file() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if !p
                .metadata()
                .is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
            {
                continue;
            }
        }
        if let Ok(p) = crate::paths::resolve(&p) {
            return Ok(p);
        }
    }
    Err(format!("{label} wrapper unavailable"))
}
pub fn lookup(kind: &str) -> Result<PathBuf, String> {
    let var = if kind == "pgk" {
        "PG_PGK"
    } else {
        "PG_CI_EVIDENCE"
    };
    let p = std::env::var_os(var).map(PathBuf::from);
    let dirs: Vec<_> =
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()).collect();
    wrapper(kind, p.as_deref(), &dirs)
}
