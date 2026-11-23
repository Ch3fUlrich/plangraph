//! Install the WSL guard from the same host policy used for fleet placement.
use serde::Deserialize;
use std::{ffi::OsString, fs, io, path::Path};

#[derive(Deserialize)]
struct Policy {
    hosts: Vec<Host>,
}

#[derive(Deserialize)]
struct Host {
    name: String,
    memory_source: Option<String>,
    cache_guard_min_free_mb: Option<u64>,
}

fn floor(policy: &[u8], name: &str) -> io::Result<Option<u64>> {
    let policy: Policy = serde_json::from_slice(policy).map_err(io::Error::other)?;
    let mut hosts = policy.hosts.iter().filter(|host| host.name == name);
    let host = hosts
        .next()
        .ok_or_else(|| io::Error::other("host missing from placement policy"))?;
    if hosts.next().is_some() {
        return Err(io::Error::other("duplicate host in placement policy"));
    }
    if host.memory_source.as_deref() != Some("windows") {
        return Ok(None);
    }
    let floor = host
        .cache_guard_min_free_mb
        .unwrap_or(pg_worker_core::wsl::CACHE_GUARD_FLOOR_MB);
    if floor == 0 {
        return Err(io::Error::other("cache_guard_min_free_mb must be positive"));
    }
    Ok(Some(floor))
}

const ROOT_BINARY: &str = "/usr/local/bin/pg-spawnd";
const UNIT: &str = "pg-wsl-cache-guard";
const SERVICE: &[u8] = include_bytes!("../../../deploy/systemd/pg-wsl-cache-guard.service");
const TIMER: &[u8] = include_bytes!("../../../deploy/systemd/pg-wsl-cache-guard.timer");

fn install(floor: u64, etc: &Path) -> io::Result<()> {
    use std::io::Write;

    let units = etc.join("systemd/system");
    fs::create_dir_all(&units)?;
    let settings = format!("PG_WSL_FREE_MB={floor}\n");
    super::unit_env::create(
        &etc.join("plangraph/wsl-cache-guard.env"),
        settings.as_bytes(),
    )?;
    for (suffix, contents) in [("service", SERVICE), ("timer", TIMER)] {
        let mut unit = tempfile::NamedTempFile::new_in(&units)?;
        unit.write_all(contents)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            unit.as_file()
                .set_permissions(fs::Permissions::from_mode(0o644))?;
        }
        unit.as_file().sync_all()?;
        unit.persist(units.join(format!("{UNIT}.{suffix}")))
            .map_err(|e| e.error)?;
    }
    Ok(())
}

fn check_root_binary(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if fs::symlink_metadata(path).is_ok_and(|meta| {
            meta.is_file()
                && meta.uid() == 0
                && meta.mode() & 0o022 == 0
                && meta.mode() & 0o111 != 0
        }) {
            return Ok(());
        }
    }
    Err(io::Error::other(format!(
        "install a root-owned, executable release of pg-spawnd at {} first",
        path.display()
    )))
}

pub(super) fn run(args: &[OsString]) -> i32 {
    let [path, host] = args else {
        eprintln!("usage: host-sync --install-wsl-guard POLICY HOST");
        return 2;
    };
    let result = (|| {
        let host = host
            .to_str()
            .ok_or_else(|| io::Error::other("invalid host name"))?;
        let Some(floor) = floor(&fs::read(path)?, host)? else {
            return Ok("SKIP WSL cache guard: memory source is not Windows");
        };
        if !cfg!(target_os = "linux") {
            return Err(io::Error::other(
                "WSL cache guard installation requires Linux",
            ));
        }
        check_root_binary(Path::new(ROOT_BINARY))?;
        install(floor, Path::new("/etc"))?;
        Ok("OK WSL cache guard: installed system units and memory setting")
    })();
    match result {
        Ok(message) => {
            println!("{message}");
            0
        }
        Err(error) => {
            eprintln!("FAIL WSL cache guard: {error}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "linux")]
    #[test]
    fn installed_guard_unit_verifies_with_root_binary_at_the_documented_path() {
        if !Path::new("/usr/bin/systemd-analyze").is_file() {
            return;
        }
        let root = tempfile::tempdir().unwrap();
        install(20_480, root.path()).unwrap();
        let units = root.path().join("systemd/system");
        let service = units.join(format!("{UNIT}.service"));
        let text = fs::read_to_string(&service).unwrap();
        assert!(text.contains(&format!("ExecStart={ROOT_BINARY} ")));
        let artifact = root.path().join(ROOT_BINARY.trim_start_matches('/'));
        fs::create_dir_all(artifact.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink("/bin/true", &artifact).unwrap();
        fs::write(
            &service,
            text.replace(ROOT_BINARY, artifact.to_str().unwrap()),
        )
        .unwrap();
        let verify = std::process::Command::new("/usr/bin/systemd-analyze")
            .arg("verify")
            .arg(service)
            .output()
            .unwrap();
        assert!(verify.status.success(), "{verify:?}");
    }

    #[test]
    fn guard_keeps_an_existing_floor() {
        let root = tempfile::tempdir().unwrap();
        let env = root.path().join("plangraph/wsl-cache-guard.env");
        install(20_480, root.path()).unwrap();
        install(18_000, root.path()).unwrap();
        assert_eq!(fs::read_to_string(env).unwrap(), "PG_WSL_FREE_MB=20480\n");
    }

    #[test]
    fn windows_policy_default_and_override_install_units_and_settings() {
        for (setting, expected) in [
            ("", 20_480),
            (", \"cache_guard_min_free_mb\": 18000", 18_000),
        ] {
            let root = tempfile::tempdir().unwrap();
            let policy =
                format!(r#"{{"hosts":[{{"name":"fixture","memory_source":"windows"{setting}}}]}}"#);
            let floor = floor(policy.as_bytes(), "fixture").unwrap().unwrap();
            assert_eq!(floor, expected);
            install(floor, root.path()).unwrap();
            assert_eq!(
                fs::read_to_string(root.path().join("plangraph/wsl-cache-guard.env")).unwrap(),
                format!("PG_WSL_FREE_MB={expected}\n")
            );
            for (suffix, contents) in [("service", SERVICE), ("timer", TIMER)] {
                assert_eq!(
                    fs::read(root.path().join(format!("systemd/system/{UNIT}.{suffix}"))).unwrap(),
                    contents
                );
            }
        }
    }
}
