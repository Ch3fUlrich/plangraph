use super::super::{replay, Result};
use std::{fs, path::Path, time::SystemTime};

pub(super) struct CargoInputs(Vec<String>);

impl CargoInputs {
    pub(super) fn prepare(
        base: &Path,
        head: &Path,
        base_sha: &str,
        head_sha: &str,
    ) -> Result<Self> {
        for root in [base, head] {
            let files = replay::git(root, &["ls-files", "-z"])?;
            freshen(root, files.split_terminator('\0'))?;
        }
        let diff = replay::git(
            head,
            &[
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                base_sha,
                head_sha,
                "--",
            ],
        )?;
        Ok(Self(
            diff.split_terminator('\0').map(str::to_owned).collect(),
        ))
    }

    /// Shared Cargo artifacts can look fresh when another checkout has older mtimes.
    pub(super) fn freshen(&self, root: &Path) -> Result<()> {
        freshen(root, self.0.iter().map(String::as_str))
    }
}

fn freshen<'a>(root: &Path, paths: impl Iterator<Item = &'a str>) -> Result<()> {
    let now = SystemTime::now();
    for path in paths {
        let path = root.join(path);
        let metadata = match path.symlink_metadata() {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.to_string()),
        };
        if metadata.is_file() && path.canonicalize().is_ok_and(|file| file.starts_with(root)) {
            fs::File::open(&path)
                .and_then(|file| file.set_modified(now))
                .map_err(|error| format!("freshen Cargo input {}: {error}", path.display()))?;
        }
    }
    Ok(())
}
