use std::path::{Path, PathBuf};

pub fn expand(path: &Path) -> PathBuf {
    path.strip_prefix("~")
        .map_or_else(|_| path.to_owned(), |p| crate::home().join(p))
}

// Canonicalize the existing prefix too: missing files must retain their ordinary
// read error, while a symlink loop must fail closed before reading anything.
pub fn resolve(path: &Path) -> std::io::Result<PathBuf> {
    let path = expand(path);
    match path.canonicalize() {
        Ok(p) => Ok(p),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            let parent = path.parent().filter(|p| !p.as_os_str().is_empty());
            let base = match parent {
                Some(p) => resolve(p)?,
                None => std::env::current_dir()?,
            };
            Ok(base.join(path.file_name().unwrap_or_default()))
        }
        Err(e) => Err(e),
    }
}

pub fn secret(path: &Path) -> bool {
    secret_at(path, &crate::home())
}
pub fn secret_at(path: &Path, home: &Path) -> bool {
    let Ok(real) = resolve(path) else { return true };
    for p in [path, real.as_path()] {
        let name = p.file_name().unwrap_or_default().to_string_lossy();
        if name.ends_with(".token") || name.ends_with(".dsn") || name.starts_with(".credentials") {
            return true;
        }
    }
    let Ok(config) = resolve(&home.join(".config/plangraph")) else {
        return true;
    };
    real.strip_prefix(config).is_ok_and(|rel| {
        rel.components()
            .next()
            .is_none_or(|c| c.as_os_str() != "liveness")
    })
}

pub fn display(path: &Path) -> String {
    let s = path.to_string_lossy();
    s.strip_prefix(r"\\?\").unwrap_or(&s).to_owned()
}
