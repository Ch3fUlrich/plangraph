#[cfg(unix)]
use super::atomic;
use super::Mode;
use serde::Deserialize;
use std::path::Path;

/// Both the downloaded archive and its installed executable are pinned.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[cfg_attr(not(unix), allow(dead_code))]
pub struct Spec {
    pub url: String,
    pub archive_sha256: String,
    pub binary_sha256: String,
    pub member: String,
}

#[cfg(unix)]
pub(super) fn install(
    path: &Path,
    old: Option<&[u8]>,
    spec: &Spec,
    mode: Mode,
) -> Result<&'static str, String> {
    use std::os::unix::fs::PermissionsExt;
    if !spec.url.starts_with("https://")
        || !valid_hash(&spec.archive_sha256)
        || !valid_hash(&spec.binary_sha256)
        || spec.member.is_empty()
        || !Path::new(&spec.member)
            .components()
            .all(|part| matches!(part, std::path::Component::Normal(_)))
    {
        return Err("invalid setup artifact".into());
    }
    let matching = old.filter(|bytes| hash(bytes) == spec.binary_sha256);
    if matching.is_some()
        && std::fs::metadata(path).is_ok_and(|m| m.permissions().mode() & 0o100 != 0)
    {
        return Ok("skipped");
    }
    let status = if old.is_some() {
        "updated"
    } else {
        "installed"
    };
    match mode {
        Mode::Check => Ok("drift"),
        Mode::DryRun => Ok(status),
        Mode::Apply => {
            let bytes = match matching {
                Some(bytes) => bytes.to_owned(),
                None => unpack(&download(&spec.url)?, spec)?,
            };
            if let Some(old) = old.filter(|_| matching.is_none()) {
                atomic::backup(path, old).map_err(|_| "artifact backup")?;
            }
            atomic::write_executable(path, &bytes).map_err(|_| "artifact write")?;
            Ok(status)
        }
    }
}

#[cfg(not(unix))]
pub(super) fn install(
    _: &Path,
    _: Option<&[u8]>,
    _: &Spec,
    _: Mode,
) -> Result<&'static str, String> {
    Err("setup executable artifacts require a Unix host".into())
}

#[cfg(unix)]
use crate::runner::download::{sha256_bytes as hash, valid_hash};

#[cfg(unix)]
fn download(url: &str) -> Result<Vec<u8>, String> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .https_only(true)
        .timeout_global(Some(std::time::Duration::from_secs(60)))
        .build()
        .into();
    let mut reply = agent
        .get(url)
        .call()
        .map_err(|e| format!("artifact download: {e}"))?;
    reply
        .body_mut()
        .with_config()
        .limit(32 * 1024 * 1024)
        .read_to_vec()
        .map_err(|e| format!("artifact body: {e}"))
}

#[cfg(unix)]
fn unpack(archive: &[u8], spec: &Spec) -> Result<Vec<u8>, String> {
    use std::io::Write;
    if hash(archive) != spec.archive_sha256 {
        return Err("setup artifact archive checksum mismatch".into());
    }
    let mut file = tempfile::NamedTempFile::new().map_err(|e| e.to_string())?;
    file.write_all(archive).map_err(|e| e.to_string())?;
    let output = std::process::Command::new("tar")
        .arg("-xOzf")
        .arg(file.path())
        .arg("--")
        .arg(&spec.member)
        .env_remove("TAR_OPTIONS")
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err("setup artifact member extraction failed".into());
    }
    if hash(&output.stdout) != spec.binary_sha256 {
        return Err("setup artifact binary checksum mismatch".into());
    }
    Ok(output.stdout)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    #[test]
    fn sccache_artifact_checks_both_digests_before_installing() {
        let root = tempfile::tempdir().unwrap();
        let archive = root.path().join("fixture.tar.gz");
        let binary = b"fixture executable";
        std::fs::write(root.path().join("sccache"), binary).unwrap();
        assert!(std::process::Command::new("tar")
            .arg("-czf")
            .arg(&archive)
            .arg("-C")
            .arg(root.path())
            .arg("sccache")
            .status()
            .unwrap()
            .success());
        let archive = std::fs::read(archive).unwrap();
        let mut spec = Spec {
            url: "https://example.invalid/bundle.tar.gz".into(),
            archive_sha256: hash(&archive),
            binary_sha256: hash(binary),
            member: "sccache".into(),
        };
        assert_eq!(unpack(&archive, &spec).unwrap(), binary);
        spec.binary_sha256 = hash(b"different");
        assert_eq!(
            unpack(&archive, &spec).unwrap_err(),
            "setup artifact binary checksum mismatch"
        );
        assert_eq!(
            unpack(b"different", &spec).unwrap_err(),
            "setup artifact archive checksum mismatch"
        );
    }
}
