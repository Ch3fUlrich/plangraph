//! Mount tables from `/proc/<pid>/mountinfo`, and the mapping of a path that a process in
//! another mount namespace names (a container's cwd or open file) to this namespace.
use std::{
    ffi::OsString,
    io,
    os::unix::ffi::OsStringExt,
    path::{Path, PathBuf},
};

/// One mountinfo row: the device, the directory of that filesystem mounted, and where.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Mount {
    pub dev: u64,
    pub root: PathBuf,
    pub point: PathBuf,
}

fn unescape(field: &[u8]) -> io::Result<PathBuf> {
    let mut bytes = Vec::with_capacity(field.len());
    let mut remaining = field;
    while let Some((&byte, tail)) = remaining.split_first() {
        if byte == b'\\' {
            let escaped = tail
                .get(..3)
                .and_then(|s| std::str::from_utf8(s).ok())
                .and_then(|s| u8::from_str_radix(s, 8).ok())
                .ok_or_else(|| io::Error::other("invalid mount escape"))?;
            bytes.push(escaped);
            remaining = &tail[3..];
        } else {
            bytes.push(byte);
            remaining = tail;
        }
    }
    Ok(PathBuf::from(OsString::from_vec(bytes)))
}

/// Parse a mountinfo table in kernel order (a later row shadows an earlier one at its point).
pub(crate) fn parse(table: &[u8]) -> io::Result<Vec<Mount>> {
    let invalid = || io::Error::other("invalid mount inventory");
    let mut mounts = Vec::new();
    for line in table.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
        let fields: Vec<_> = line.split(|b| *b == b' ').take(5).collect();
        let [_, _, dev, root, point] = fields[..] else {
            return Err(invalid());
        };
        let (major, minor) = std::str::from_utf8(dev)
            .ok()
            .and_then(|dev| dev.split_once(':'))
            .and_then(|(major, minor)| Some((major.parse().ok()?, minor.parse().ok()?)))
            .ok_or_else(invalid)?;
        mounts.push(Mount {
            dev: libc::makedev(major, minor),
            root: unescape(root)?,
            point: unescape(point)?,
        });
    }
    Ok(mounts)
}

pub(crate) fn read(pid: &Path) -> io::Result<Vec<Mount>> {
    parse(&std::fs::read(pid.join("mountinfo"))?)
}

/// The paths in this namespace (`ours`) where `name`, as a process with mount table `theirs`
/// names it, is reachable. `None` when no mount here shows `dev`, the device of the named
/// object: nothing under this namespace's paths can be that object.
pub(crate) fn host_paths(
    name: &Path,
    dev: u64,
    theirs: &[Mount],
    ours: &[Mount],
) -> Option<Vec<PathBuf>> {
    if !ours.iter().any(|mount| mount.dev == dev) {
        return None;
    }
    let Some(mount) = theirs
        .iter()
        .rev()
        .filter(|mount| name.starts_with(&mount.point))
        .max_by_key(|mount| mount.point.components().count())
    else {
        return Some(vec![]);
    };
    let source = mount.root.join(name.strip_prefix(&mount.point).unwrap());
    Some(
        ours.iter()
            .filter(|host| host.dev == mount.dev)
            .filter_map(|host| Some(host.point.join(source.strip_prefix(&host.root).ok()?)))
            .collect(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn container_bind_mount_maps_to_the_host_path() {
        let disk = libc::makedev(8, 2);
        let overlay = libc::makedev(0, 74);
        let theirs = parse(
            b"1 0 0:74 / / rw - overlay overlay rw\n\
              2 1 8:2 /data/u/.data /.data rw - ext4 /dev/sda2 rw\n\
              3 1 8:2 /data/u/w\\040x /app rw - ext4 /dev/sda2 rw\n",
        )
        .unwrap();
        let ours = parse(b"20 1 8:2 / / rw - ext4 /dev/sda2 rw\n").unwrap();
        assert_eq!(theirs[2].root, Path::new("/data/u/w x"));
        assert_eq!(
            host_paths(Path::new("/.data/db"), disk, &theirs, &ours),
            Some(vec![PathBuf::from("/data/u/.data/db")])
        );
        assert_eq!(
            host_paths(Path::new("/app/rust/target"), disk, &theirs, &ours),
            Some(vec![PathBuf::from("/data/u/w x/rust/target")])
        );
        // The container's own overlay is not mounted here: its files are unreachable.
        assert_eq!(
            host_paths(Path::new("/usr/bin/node"), overlay, &theirs, &ours),
            None
        );
        // A device mounted here but no mapping: the caller must fail closed on an empty list.
        let other = parse(b"20 1 8:3 / / rw - ext4 /dev/sda3 rw\n21 1 8:2 /srv /srv rw - x y rw\n")
            .unwrap();
        assert_eq!(
            host_paths(Path::new("/app/x"), disk, &theirs, &other),
            Some(vec![])
        );
        assert!(parse(b"1 0 bad / / rw\n").is_err());
    }
}
