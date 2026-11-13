//! No file payload is ever opened. All decisions use names and metadata.
use crate::atomic;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

fn directory(path: &Path) -> io::Result<()> {
    for ancestor in path.ancestors() {
        if fs::symlink_metadata(ancestor).is_ok_and(|m| m.file_type().is_symlink()) {
            return Err(io::Error::other("symlink directory refused"));
        }
    }
    if path.exists() {
        if !path.is_dir() {
            return Err(io::Error::other("directory required"));
        }
        return Ok(());
    }
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}
pub fn destination(name: &str) -> (String, String, bool) {
    if !name.contains(['\r', '\n']) {
        if let Some((owner, rest)) = name.split_once("__") {
            if owner.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
                && owner
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
                && !rest.is_empty()
            {
                return (
                    owner.into(),
                    if rest.starts_with(['.', '-']) {
                        format!("_{rest}")
                    } else {
                        rest.into()
                    },
                    false,
                );
            }
        }
    }
    ("unsorted".into(), name.into(), true)
}

pub fn run(root: &Path, fetch: impl FnOnce(&Path) -> bool) -> io::Result<usize> {
    directory(root)?;
    directory(&root.join("incoming"))?;
    directory(&root.join("unsorted"))?;
    let _lock = atomic::lock(&root.join("fetcher"))?;
    if !fetch(&root.join("incoming")) {
        eprintln!("taildrop-sort: nothing fetched; fetch unavailable");
        return Ok(0);
    }
    let entries = fs::read_dir(root.join("incoming"))?.collect::<Result<Vec<_>, _>>()?;
    let mut moved = 0;
    for entry in entries {
        if !entry.file_type()?.is_file() {
            continue;
        }
        let name = entry.file_name();
        let mapped = name.to_str().map(destination);
        // Preserve non-UTF8 delivered names as unsorted too.
        let (owner, rest, unsorted) = mapped
            .map(|(o, r, u)| (o, std::ffi::OsString::from(r), u))
            .unwrap_or_else(|| ("unsorted".into(), name.clone(), true));
        let target_dir = root.join(owner);
        directory(&target_dir)?;
        let mut target = target_dir.join(&rest);
        let mut collision = 0;
        loop {
            match fs::hard_link(entry.path(), &target) {
                Ok(()) => break,
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                    collision += 1;
                    let mut numbered = rest.clone();
                    numbered.push(format!(".{collision}"));
                    target = target_dir.join(numbered);
                }
                Err(e) => return Err(e),
            }
        }
        fs::remove_file(entry.path())?;
        moved += 1;
        if unsorted {
            let log = root.join("sort.log");
            atomic::regular(&log)?;
            let mut options = fs::OpenOptions::new();
            options.create(true).append(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let line = name.to_string_lossy().replace(['\r', '\n'], " ");
            writeln!(
                options.open(log)?,
                "{} unsorted {line}",
                chrono::Utc::now().to_rfc3339()
            )?;
        }
    }
    Ok(moved)
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    if !args.is_empty() && args != ["--dry-run"] {
        return 2;
    }
    let root = std::env::var_os("PLANGRAPH_TAILDROP_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join("fleet/taildrop"));
    if !args.is_empty() {
        println!("taildrop-sort check: one fetcher; filename-only filing; no fetch or writes");
        return 0;
    }
    match run(&root, |incoming| {
        Command::new("tailscale")
            .args(["file", "get", "--wait=false", "--conflict=rename"])
            .arg(incoming)
            .status()
            .is_ok_and(|s| s.success())
    }) {
        Ok(count) => {
            println!("taildrop-sort filed={count}");
            0
        }
        Err(_) => {
            eprintln!("taildrop-sort refused lock, directory or move failure");
            3
        }
    }
}
