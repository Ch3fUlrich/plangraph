//! Verify and repair PATH symlinks for handoff-check and the fleet tools.
use serde_json::Value;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

pub const FLEET_TOOLS: [&str; 3] = ["handoff-check", "resume-digest", "fleet-name"];

static TEMP_SEQ: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Options {
    pub data_dir: Option<PathBuf>,
    pub bin_dir: Option<PathBuf>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Env {
    pub path: Option<OsString>,
    pub home: Option<PathBuf>,
    pub xdg_config_home: Option<PathBuf>,
    pub pg_data_dir: Option<PathBuf>,
    pub host_name: Option<String>,
}

impl Env {
    pub fn from_process() -> Self {
        let non_empty = |key: &str| std::env::var_os(key).filter(|v| !v.is_empty());
        Self {
            path: std::env::var_os("PATH"),
            home: non_empty("HOME")
                .or_else(|| non_empty("USERPROFILE"))
                .map(PathBuf::from),
            xdg_config_home: non_empty("XDG_CONFIG_HOME").map(PathBuf::from),
            pg_data_dir: non_empty("PG_DATA_DIR").map(PathBuf::from),
            host_name: non_empty("PLANGRAPH_HOST_NAME").map(|v| v.to_string_lossy().into_owned()),
        }
    }
}

pub fn parse_args(args: &[OsString]) -> Result<Options, String> {
    let mut options = Options::default();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        let flag = arg
            .to_str()
            .ok_or_else(|| "host-sync: invalid UTF-8 argument".to_string())?;
        let slot = match flag {
            "--data-dir" => &mut options.data_dir,
            "--bin-dir" => &mut options.bin_dir,
            _ => return Err(format!("host-sync: unknown argument {flag}")),
        };
        let value = iter
            .next()
            .filter(|v| !v.is_empty())
            .ok_or_else(|| format!("host-sync: {flag} requires a path"))?;
        *slot = Some(PathBuf::from(value));
    }
    Ok(options)
}

pub fn run_cli(args: &[OsString]) -> i32 {
    let options = match parse_args(args) {
        Ok(options) => options,
        Err(error) => {
            eprintln!("{error}");
            eprintln!("usage: host-sync [--data-dir PATH] [--bin-dir PATH]");
            return 2;
        }
    };
    let (code, lines) = sync(&options, &Env::from_process());
    for line in lines {
        println!("{line}");
    }
    code
}

pub fn cli_main() -> i32 {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    run_cli(&args)
}

pub fn sync(options: &Options, env: &Env) -> (i32, Vec<String>) {
    let mut lines = Vec::with_capacity(FLEET_TOOLS.len());
    let mut missing = Vec::new();

    for tool in FLEET_TOOLS {
        if let Some(resolved) = resolve_on_path(tool, env.path.as_deref()) {
            lines.push(format!("OK {tool}: {}", resolved.display()));
        } else {
            missing.push(tool);
        }
    }

    if missing.is_empty() {
        return (0, lines);
    }

    let bin_dir = match resolve_bin_dir(options, env) {
        Ok(dir) => dir,
        Err(reason) => {
            for tool in missing {
                lines.push(format!("FAIL {tool}: missing from PATH; {reason}"));
            }
            return (1, lines);
        }
    };

    if !dir_on_path(&bin_dir, env.path.as_deref()) {
        for tool in missing {
            lines.push(format!(
                "FAIL {tool}: missing from PATH; target bin dir {} is not on PATH",
                bin_dir.display()
            ));
        }
        return (1, lines);
    }

    let data_dir = match resolve_data_dir(options, env) {
        Ok(dir) => dir,
        Err(reason) => {
            for tool in missing {
                lines.push(format!("FAIL {tool}: missing from PATH; {reason}"));
            }
            return (1, lines);
        }
    };

    let mut failed = false;
    for tool in missing {
        match link_and_verify(tool, &data_dir, &bin_dir, env.path.as_deref()) {
            Ok(line) => lines.push(line),
            Err(reason) => {
                failed = true;
                lines.push(format!("FAIL {tool}: missing from PATH; {reason}"));
            }
        }
    }

    (i32::from(failed), lines)
}

fn link_and_verify(
    tool: &str,
    data_dir: &Path,
    bin_dir: &Path,
    path_env: Option<&OsStr>,
) -> Result<String, String> {
    let src = data_dir.join("router/bin").join(tool);
    if !is_executable(&src) {
        return Err(format!(
            "source executable missing or not executable at {}",
            src.display()
        ));
    }
    let abs_src = std::path::absolute(&src)
        .map_err(|e| format!("cannot resolve source path {}: {e}", src.display()))?;
    fs::create_dir_all(bin_dir)
        .map_err(|e| format!("cannot create bin dir {}: {e}", bin_dir.display()))?;
    let dst = bin_dir.join(tool);
    ensure_link_slot_safe(&dst)?;
    atomic_symlink(&abs_src, &dst, tool)?;
    if resolve_on_path(tool, path_env).is_none() {
        return Err(format!(
            "linked at {} but still does not resolve on PATH",
            dst.display()
        ));
    }
    Ok(format!(
        "LINKED {tool}: {} -> {}",
        dst.display(),
        abs_src.display()
    ))
}

fn ensure_link_slot_safe(dst: &Path) -> Result<(), String> {
    match fs::symlink_metadata(dst) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        // Replace broken symlinks left behind by retired checkouts.
        Ok(meta) if meta.file_type().is_symlink() && fs::metadata(dst).is_err() => Ok(()),
        Ok(_) => Err(format!(
            "destination {} exists and is not a dangling symlink",
            dst.display()
        )),
        Err(e) => Err(format!("cannot inspect {}: {e}", dst.display())),
    }
}

fn atomic_symlink(src: &Path, dst: &Path, tool: &str) -> Result<(), String> {
    let parent = dst
        .parent()
        .ok_or_else(|| format!("missing parent for {}", dst.display()))?;
    let seq = TEMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let tmp = parent.join(format!(".host-sync.{tool}.{}.{seq}", std::process::id()));
    let _ = fs::remove_file(&tmp);
    create_symlink(src, &tmp)
        .map_err(|e| format!("cannot create symlink {}: {e}", dst.display()))?;
    if let Err(e) = fs::rename(&tmp, dst) {
        let _ = fs::remove_file(&tmp);
        return Err(format!("cannot install symlink {}: {e}", dst.display()));
    }
    Ok(())
}

#[cfg(unix)]
fn create_symlink(src: &Path, dst: &Path) -> io::Result<()> {
    std::os::unix::fs::symlink(src, dst)
}

#[cfg(windows)]
fn create_symlink(src: &Path, dst: &Path) -> io::Result<()> {
    std::os::windows::fs::symlink_file(src, dst)
}

fn resolve_bin_dir(options: &Options, env: &Env) -> Result<PathBuf, String> {
    if let Some(dir) = &options.bin_dir {
        return Ok(dir.clone());
    }
    env.home
        .as_ref()
        .map(|home| home.join(".local/bin"))
        .ok_or_else(|| "HOME is not set and --bin-dir was not provided".to_string())
}

fn resolve_data_dir(options: &Options, env: &Env) -> Result<PathBuf, String> {
    if let Some(dir) = &options.data_dir {
        return require_checkout_dir(dir, "--data-dir");
    }
    if let Some(dir) = &env.pg_data_dir {
        return require_checkout_dir(dir, "PG_DATA_DIR");
    }
    if let Some(dir) = data_dir_from_hosts_json(
        env.home.as_deref(),
        env.xdg_config_home.as_deref(),
        env.host_name.as_deref(),
    ) {
        return require_checkout_dir(&dir, "hosts.json repos.plangraph-data.root");
    }
    if let Some(home) = &env.home {
        let candidate = home.join("code/plangraph-data");
        if candidate.is_dir() {
            return Ok(candidate);
        }
    }
    Err("plangraph-data checkout not found".to_string())
}

fn require_checkout_dir(dir: &Path, source: &str) -> Result<PathBuf, String> {
    if dir.is_dir() {
        Ok(dir.to_path_buf())
    } else {
        Err(format!(
            "plangraph-data checkout ({source}) not found at {}",
            dir.display()
        ))
    }
}

fn data_dir_from_hosts_json(
    home: Option<&Path>,
    xdg_config_home: Option<&Path>,
    host_name: Option<&str>,
) -> Option<PathBuf> {
    let config_root = xdg_config_home
        .map(Path::to_path_buf)
        .or_else(|| home.map(|h| h.join(".config")))?;
    let raw = fs::read_to_string(config_root.join("plangraph/hosts.json")).ok()?;
    let registry: Value = serde_json::from_str(&raw).ok()?;
    let target = host_name
        .filter(|s| !s.is_empty())
        .or_else(|| registry.get("self")?.as_str())?;
    let hosts = registry.get("hosts")?.as_array()?;
    let host = hosts.iter().find(|entry| {
        let id_match = entry
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(|id| id.eq_ignore_ascii_case(target));
        let alias_match = entry
            .get("aliases")
            .and_then(Value::as_array)
            .is_some_and(|aliases| {
                aliases
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|alias| alias.eq_ignore_ascii_case(target))
            });
        id_match || alias_match
    })?;
    host.get("repos")?
        .get("plangraph-data")?
        .get("root")?
        .as_str()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

pub fn resolve_on_path(tool: &str, path_env: Option<&OsStr>) -> Option<PathBuf> {
    let path_env = path_env?;
    for dir in std::env::split_paths(path_env) {
        if dir.as_os_str().is_empty() {
            continue;
        }
        let candidate = dir.join(tool);
        if is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

fn is_executable(path: &Path) -> bool {
    let Ok(meta) = fs::metadata(path) else {
        return false;
    };
    if !meta.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        meta.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn dir_on_path(target: &Path, path_env: Option<&OsStr>) -> bool {
    let Some(path_env) = path_env else {
        return false;
    };
    let target_norm = normalize_components(target);
    let target_canon = best_effort_canonical(target);
    std::env::split_paths(path_env)
        .filter(|entry| !entry.as_os_str().is_empty())
        .any(|entry| {
            normalize_components(&entry) == target_norm
                || target_canon
                    .as_ref()
                    .zip(best_effort_canonical(&entry))
                    .is_some_and(|(a, b)| *a == b)
        })
}

fn normalize_components(path: &Path) -> Vec<Component<'_>> {
    path.components()
        .filter(|c| !matches!(c, Component::CurDir))
        .collect()
}

fn best_effort_canonical(path: &Path) -> Option<PathBuf> {
    let abs = std::path::absolute(path).ok()?;
    let mut missing = Vec::new();
    let mut cur = abs.as_path();
    loop {
        if let Ok(canon) = fs::canonicalize(cur) {
            return Some(
                missing
                    .into_iter()
                    .rev()
                    .fold(canon, |acc, part| acc.join(part)),
            );
        }
        let name = cur.file_name()?;
        missing.push(name);
        cur = cur.parent()?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_exec(path: &Path) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, "#!/bin/sh\nexit 0\n").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
        }
    }

    #[test]
    fn parse_args_validates_flags() {
        let args: Vec<OsString> = ["--data-dir", "/a", "--bin-dir", "/b"]
            .into_iter()
            .map(OsString::from)
            .collect();
        assert_eq!(
            parse_args(&args).unwrap(),
            Options {
                data_dir: Some(PathBuf::from("/a")),
                bin_dir: Some(PathBuf::from("/b")),
            }
        );
        assert!(parse_args(&[OsString::from("--data-dir")]).is_err());
        assert!(parse_args(&[OsString::from("--bogus"), OsString::from("x")]).is_err());
        let unknown = parse_args(&[OsString::from("--bogus")]).unwrap_err();
        assert!(unknown.contains("unknown argument --bogus"), "{unknown}");
    }

    #[test]
    fn trailing_slash_and_missing_dir_on_path_match() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join(".local/bin");
        let with_slash = OsString::from(format!("{}/", target.display()));
        assert!(dir_on_path(&target, Some(&with_slash)));
        assert!(!dir_on_path(&target, Some(OsStr::new("/usr/bin"))));
        #[cfg(unix)]
        {
            let real_home = root.path().join("real-home");
            let link_home = root.path().join("link-home");
            fs::create_dir_all(&real_home).unwrap();
            std::os::unix::fs::symlink(&real_home, &link_home).unwrap();
            assert!(dir_on_path(
                &link_home.join(".local/bin"),
                Some(real_home.join(".local/bin").as_os_str()),
            ));
        }
    }

    #[test]
    fn host_registry_resolves_plangraph_data_root_case_insensitively() {
        let root = tempfile::tempdir().unwrap();
        let config_dir = root.path().join("xdg/plangraph");
        fs::create_dir_all(&config_dir).unwrap();
        let data = root.path().join("custom-data");
        make_exec(&data.join("router/bin/handoff-check"));
        make_exec(&data.join("router/bin/resume-digest"));
        make_exec(&data.join("router/bin/fleet-name"));
        fs::write(
            config_dir.join("hosts.json"),
            serde_json::json!({
                "version": 1,
                "self": "workstation",
                "hosts": [{
                    "id": "workstation",
                    "aliases": ["workst"],
                    "repos": {
                        "plangraph-data": {"root": data}
                    }
                }]
            })
            .to_string(),
        )
        .unwrap();
        let bin = root.path().join(".local/bin");
        let env = Env {
            path: Some(OsString::from(bin.as_os_str())),
            home: Some(root.path().to_path_buf()),
            xdg_config_home: Some(root.path().join("xdg")),
            pg_data_dir: None,
            host_name: Some("WORKST".into()),
        };
        assert_eq!(
            data_dir_from_hosts_json(Some(root.path()), Some(&root.path().join("xdg")), None,),
            Some(data.clone())
        );
        let (code, lines) = sync(&Options::default(), &env);
        assert_eq!(code, 0, "{lines:?}");
        assert!(lines.iter().all(|l| l.starts_with("LINKED ")), "{lines:?}");
    }
}
