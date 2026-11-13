//! Explicit allowlist for non-secret moves. Secret/container/profile edits are operator-only.
use crate::{atomic, setup::Mode};
use serde::Deserialize;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    pub name: String,
    pub old: String,
    pub new: String,
    pub kind: String,
    pub consumers: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Gate {
    pub task: String,
    pub requires: Vec<String>,
    pub gates: Vec<String>,
}
fn relative(path: &str) -> bool {
    !path.is_empty()
        && Path::new(path)
            .components()
            .all(|c| matches!(c, Component::Normal(_)))
}
fn resolve(home: &Path, data: &Path, text: &str) -> io::Result<PathBuf> {
    let (base, rest) = text
        .strip_prefix("@DATA@/")
        .map(|s| (data, s))
        .unwrap_or((home, text));
    if !relative(rest) {
        return Err(io::Error::other("unsafe manifest path"));
    }
    Ok(base.join(rest))
}
fn present(path: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(path) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}
fn ancestors(path: &Path) -> io::Result<()> {
    for ancestor in path.ancestors() {
        match fs::symlink_metadata(ancestor) {
            Ok(m) if m.file_type().is_symlink() => {
                return Err(io::Error::other("symlink ancestor refused"))
            }
            Ok(_) => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => return Err(e),
        }
    }
    Ok(())
}
fn is_shim(old: &Path, new: &Path) -> io::Result<bool> {
    let target = match fs::read_link(old) {
        Ok(target) => target,
        Err(e) if [io::ErrorKind::NotFound, io::ErrorKind::InvalidInput].contains(&e.kind()) => {
            return Ok(false)
        }
        Err(e) => return Err(e),
    };
    let absolute = if target.is_absolute() {
        target
    } else {
        old.parent().unwrap().join(target)
    };
    if !present(&absolute)? || !present(new)? {
        return Ok(false);
    }
    Ok(fs::canonicalize(absolute)? == fs::canonicalize(new)?)
}
#[derive(Debug)]
struct ActionError {
    partial: bool,
}
impl From<io::Error> for ActionError {
    fn from(_: io::Error) -> Self {
        Self { partial: false }
    }
}
fn commit_move(
    old: &Path,
    new: &Path,
    source: bool,
    link: impl FnOnce(&Path, &Path) -> io::Result<()>,
) -> Result<(), ActionError> {
    if source {
        fs::rename(old, new)?;
    }
    // A failed shim never writes back over a concurrently recreated old path.
    // Keep the destination intact; the next apply resumes or reports a conflict.
    link(old, new).map_err(|_| ActionError { partial: source })
}
#[cfg(unix)]
fn shim(old: &Path, new: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(new, old)
}
#[cfg(windows)]
fn shim(old: &Path, new: &Path) -> io::Result<()> {
    if new.is_dir() {
        std::os::windows::fs::symlink_dir(new, old)
    } else {
        std::os::windows::fs::symlink_file(new, old)
    }
}
#[cfg(not(any(unix, windows)))]
fn shim(_: &Path, _: &Path) -> io::Result<()> {
    Err(io::Error::other("symlinks unsupported"))
}
#[cfg(unix)]
fn private_secret(path: &Path) -> io::Result<bool> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() {
        return Ok(false);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let expected = if metadata.is_dir() { 0o700 } else { 0o600 };
        if metadata.permissions().mode() & 0o777 != expected {
            return Ok(false);
        }
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)? {
            if !private_secret(&entry?.path())? {
                return Ok(false);
            }
        }
    }
    Ok(true)
}
#[cfg(not(unix))]
fn private_secret(_: &Path) -> io::Result<bool> {
    Ok(false)
} // ACL attestation is operator-only.
fn action(
    home: &Path,
    data: &Path,
    entry: &Entry,
    mode: Mode,
    link: fn(&Path, &Path) -> io::Result<()>,
) -> Result<&'static str, ActionError> {
    let old = resolve(home, data, &entry.old)?;
    let new = resolve(home, data, &entry.new)?;
    ancestors(&new)?;
    // Operator root shims can make a child path resolve directly to its new path.
    if present(&old)? && present(&new)? && fs::canonicalize(&old)? == fs::canonicalize(&new)? {
        if entry.kind == "secret" && !private_secret(&new)? {
            return Ok("operator-only");
        }
        return Ok("skipped");
    }
    ancestors(old.parent().unwrap())?;
    let old_exists = present(&old)?;
    if !old_exists && !present(&new)? {
        return Ok("skipped");
    }
    if is_shim(&old, &new)? {
        if entry.kind == "secret" && !private_secret(&new)? {
            return Ok("operator-only");
        }
        return Ok("skipped");
    }
    if !matches!(entry.kind.as_str(), "non-secret" | "state") {
        return Ok("operator-only");
    }
    if old_exists && present(&new)? {
        return Err(io::Error::other("migration conflict; reconcile without overwrite").into());
    }
    if fs::symlink_metadata(&old).is_ok_and(|m| m.file_type().is_symlink()) {
        return Err(io::Error::other("unexpected shim").into());
    }
    if mode == Mode::Check {
        return Ok("drift");
    }
    if mode == Mode::DryRun {
        return Ok("would-move");
    }
    fs::create_dir_all(new.parent().unwrap())?;
    fs::create_dir_all(old.parent().unwrap())?;
    let _lock = atomic::lock(&new)?;
    // Recheck after lock. Never overwrite a destination created by another mover.
    if is_shim(&old, &new)? {
        return Ok("skipped");
    }
    let source = present(&old)?;
    if source && present(&new)? {
        return Err(io::Error::other("migration conflict").into());
    }
    commit_move(&old, &new, source, link)?;
    Ok("moved")
}
pub fn run(home: &Path, data: &Path, manifest: &str, mode: Mode) -> (i32, Vec<String>) {
    run_with(home, data, manifest, mode, shim)
}
fn run_with(
    home: &Path,
    data: &Path,
    manifest: &str,
    mode: Mode,
    link: fn(&Path, &Path) -> io::Result<()>,
) -> (i32, Vec<String>) {
    let manifest: crate::setup::Manifest = match toml::from_str(manifest) {
        Ok(m) => m,
        Err(_) => return (2, vec!["namespace refused invalid manifest".into()]),
    };
    if !home.is_absolute() || !data.is_absolute() {
        return (2, vec!["namespace refused absolute roots required".into()]);
    }
    let mut code = 0;
    let mut moved = false;
    let mut rows = Vec::new();
    for entry in manifest.namespace {
        match action(home, data, &entry, mode, link) {
            Ok(status) => {
                moved |= status == "moved";
                if status != "skipped" && mode == Mode::Check {
                    code = code.max(1);
                }
                if status == "operator-only" && mode == Mode::Apply {
                    code = code.max(3);
                }
                rows.push(format!("namespace {} {status}", entry.name));
            }
            Err(error) => {
                moved |= error.partial;
                code = code.max(2);
                rows.push(format!(
                    "namespace {} refused conflict or unsafe path",
                    entry.name
                ));
            }
        }
    }
    if mode == Mode::Apply && moved && code != 0 {
        code = 3;
    }
    (code, rows)
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    let mode = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["check"] => Mode::Check,
        ["apply", "--dry-run"] => Mode::DryRun,
        ["apply"] => Mode::Apply,
        ["inventory"] => {
            let manifest: crate::setup::Manifest = toml::from_str(crate::setup::MANIFEST).unwrap();
            for entry in manifest.namespace {
                println!(
                    "{} {} -> {} kind={} consumers={}",
                    entry.name,
                    entry.old,
                    entry.new,
                    entry.kind,
                    entry.consumers.join(",")
                );
            }
            for gate in manifest.operator_tasks {
                println!(
                    "{} requires={} gates={}",
                    gate.task,
                    gate.requires.join(","),
                    gate.gates.join(",")
                );
            }
            return 0;
        }
        _ => return 2,
    };
    let Some(data) = std::env::var_os("PLANGRAPH_DATA_ROOT").map(PathBuf::from) else {
        eprintln!("namespace refused PLANGRAPH_DATA_ROOT required");
        return 2;
    };
    let (code, rows) = run(home, &data, crate::setup::MANIFEST, mode);
    for row in rows {
        println!("{row}");
    }
    code
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn shim_failure_preserves_new_and_recreated_old() {
        let root = tempfile::tempdir().unwrap();
        let old = root.path().join("old");
        let new = root.path().join("new");
        fs::write(&old, b"original").unwrap();
        assert!(commit_move(&old, &new, true, |old, _| {
            fs::write(old, b"concurrent").unwrap();
            Err(io::Error::from(io::ErrorKind::AlreadyExists))
        })
        .is_err());
        assert_eq!(fs::read(old).unwrap(), b"concurrent");
        assert_eq!(fs::read(new).unwrap(), b"original");
    }
    #[test]
    fn interrupted_single_move_reports_partial_failure() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        let data = root.path().join("data");
        fs::create_dir(&home).unwrap();
        fs::write(home.join("old"), b"original").unwrap();
        let manifest = "files=[]\n[[namespace]]\nname='fixture'\nold='old'\nnew='@DATA@/new'\nkind='state'\nconsumers=[]\n";
        let (code, _) = run_with(&home, &data, manifest, Mode::Apply, |old, _| {
            fs::write(old, b"concurrent").unwrap();
            Err(io::Error::from(io::ErrorKind::AlreadyExists))
        });
        assert_eq!(code, 3);
        assert_eq!(fs::read(data.join("new")).unwrap(), b"original");
    }
    #[test]
    fn metadata_errors_are_not_absence() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("loop");
        std::os::unix::fs::symlink("loop", &path).unwrap();
        assert!(present(&path.join("child")).is_err());
    }
}
