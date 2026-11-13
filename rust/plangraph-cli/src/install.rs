//! Package ownership stays here; system changes require explicit --yes.
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub const REQUIREMENTS: &str = include_str!("../../../deploy/requirements-host.json");
#[derive(Deserialize)]
struct Requirements {
    components: BTreeMap<String, Vec<String>>,
}
struct Package {
    id: &'static str,
    binary: &'static str,
    source: Source,
}
enum Source {
    Apt(&'static [&'static str]),
    Npm(&'static str),
    Script(&'static str, bool),
}
const PACKAGES: &[Package] = &[
    Package {
        id: "git",
        binary: "git",
        source: Source::Apt(&["git"]),
    },
    Package {
        id: "python",
        binary: "python3",
        source: Source::Apt(&[
            "python3",
            "python3-pip",
            "python3-venv",
            "python3-setuptools",
            "python3-wheel",
        ]),
    },
    Package {
        id: "nodejs",
        binary: "node",
        source: Source::Script("https://deb.nodesource.com/setup_lts.x", true),
    },
    Package {
        id: "tailscale",
        binary: "tailscale",
        source: Source::Script("https://tailscale.com/install.sh", true),
    },
    Package {
        id: "opencode-cli",
        binary: "opencode",
        source: Source::Npm("@opencode/cli"),
    },
    Package {
        id: "claude-code",
        binary: "claude",
        source: Source::Npm("@anthropic-ai/claude-code"),
    },
    Package {
        id: "qwen-code",
        binary: "qwen",
        source: Source::Npm("@qwen-code/qwen-code"),
    },
    Package {
        id: "gemini-cli",
        binary: "gemini",
        source: Source::Npm("@google/gemini-cli"),
    },
    Package {
        id: "qoder-cli",
        binary: "qodercli",
        source: Source::Script("https://qoder.com/install", false),
    },
];
pub trait Host {
    fn detected(&mut self, binary: &str) -> bool;
    fn apt(&mut self, packages: &[&str]) -> Result<(), String>;
    fn npm(&mut self, package: &str) -> Result<(), String>;
    fn script(&mut self, url: &str, privileged: bool) -> Result<(), String>;
}
pub fn select(only: Option<&str>) -> Result<Vec<&'static str>, String> {
    let requirements: Requirements =
        serde_json::from_str(REQUIREMENTS).map_err(|_| "invalid requirements")?;
    let mut requested = BTreeSet::new();
    for name in only.unwrap_or("core,agents").split(',') {
        if let Some(group) = requirements.components.get(name) {
            requested.extend(group.iter().cloned());
        } else if PACKAGES.iter().any(|p| p.id == name) {
            requested.insert(name.into());
        } else {
            return Err("unknown install component".into());
        }
    }
    if PACKAGES
        .iter()
        .any(|p| requested.contains(p.id) && matches!(p.source, Source::Npm(_)))
    {
        requested.insert("nodejs".into());
    }
    if requested
        .iter()
        .any(|id| !PACKAGES.iter().any(|p| p.id == id))
    {
        return Err("unowned requirement".into());
    }
    Ok(PACKAGES
        .iter()
        .filter(|p| requested.contains(p.id))
        .map(|p| p.id)
        .collect())
}
pub fn run(
    host: &mut impl Host,
    only: Option<&str>,
    dry_run: bool,
    check: bool,
) -> (i32, Vec<String>) {
    let selected = match select(only) {
        Ok(selected) => selected,
        Err(error) => return (2, vec![format!("install refused {error}")]),
    };
    let mut rows = Vec::new();
    let mut failed = false;
    let mut drift = false;
    for id in selected {
        let package = PACKAGES.iter().find(|p| p.id == id).unwrap();
        if host.detected(package.binary) {
            rows.push(format!("{id} skipped detected"));
            continue;
        }
        drift = true;
        if dry_run || check {
            rows.push(format!(
                "{id} {} missing",
                if check { "drift" } else { "would-install" }
            ));
            continue;
        }
        let result = match package.source {
            Source::Apt(packages) => host.apt(packages),
            Source::Npm(name) => host.npm(name),
            Source::Script(url, privileged) => host.script(url, privileged),
        };
        if result.is_ok() && host.detected(package.binary) {
            rows.push(format!("{id} installed verified"));
        } else {
            failed = true;
            rows.push(format!("{id} refused installation or verification failed"));
            break;
        }
    }
    (
        if failed {
            3
        } else if check && drift {
            1
        } else {
            0
        },
        rows,
    )
}
pub fn validate_script(bytes: &[u8]) -> bool {
    bytes.starts_with(b"#!") && bytes.len() <= 2 * 1024 * 1024 && !bytes.contains(&0)
}
struct SystemHost {
    home: PathBuf,
    root: bool,
    apt_updated: bool,
}
impl SystemHost {
    fn command(&self, program: &str, privileged: bool) -> Command {
        let mut cmd = if privileged && !self.root {
            let mut cmd = Command::new("sudo");
            cmd.args(["-n", "--", program]);
            cmd
        } else {
            Command::new(program)
        };
        let mut paths = vec![self.home.join(".local/bin")];
        if let Some(path) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&path));
        }
        if let Ok(path) = std::env::join_paths(paths) {
            cmd.env("PATH", path);
        }
        cmd
    }
    fn execute(&self, program: &str, args: &[&str], privileged: bool) -> Result<(), String> {
        self.command(program, privileged)
            .args(args)
            .status()
            .map_err(|_| "command unavailable".into())
            .and_then(|s| {
                if s.success() {
                    Ok(())
                } else {
                    Err("command failed".into())
                }
            })
    }
}
impl Host for SystemHost {
    fn detected(&mut self, binary: &str) -> bool {
        let local = self.home.join(".local/bin").join(binary);
        let program = if local.exists() {
            local.to_string_lossy().into_owned()
        } else {
            binary.into()
        };
        let flag = if binary == "tailscale" {
            "version"
        } else {
            "--version"
        };
        let result = self
            .command(&program, false)
            .arg(flag)
            .stdin(Stdio::null())
            .output();
        let Ok(output) = result else {
            return false;
        };
        if !output.status.success() {
            return false;
        }
        if binary == "python3" {
            return self.command(&program,false)
                .args(["-c", "import venv, ensurepip, pip, wheel; import importlib.util; assert importlib.util.find_spec('setuptools')"])
                .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
                .status().is_ok_and(|s|s.success());
        }
        if binary == "node" {
            return String::from_utf8_lossy(&output.stdout)
                .trim()
                .trim_start_matches('v')
                .split('.')
                .next()
                .and_then(|s| s.parse::<u32>().ok())
                .is_some_and(|n| n >= 20)
                && self
                    .command("npm", false)
                    .arg("--version")
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .is_ok_and(|s| s.success());
        }
        true
    }
    fn apt(&mut self, packages: &[&str]) -> Result<(), String> {
        if !self.apt_updated {
            self.execute("apt-get", &["update"], true)?;
            self.apt_updated = true;
        }
        let mut args = vec!["install", "-y", "--"];
        args.extend_from_slice(packages);
        self.execute("apt-get", &args, true)
    }
    fn npm(&mut self, package: &str) -> Result<(), String> {
        let prefix = self.home.join(".local");
        self.execute(
            "npm",
            &[
                "install",
                "--global",
                "--prefix",
                prefix.to_str().ok_or("invalid home")?,
                "--",
                package,
            ],
            false,
        )
    }
    fn script(&mut self, url: &str, privileged: bool) -> Result<(), String> {
        self.apt(&["curl", "ca-certificates"])?;
        let file = tempfile::NamedTempFile::new().map_err(|_| "temporary file unavailable")?;
        self.execute(
            "curl",
            &[
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--max-time",
                "120",
                "--max-filesize",
                "2097152",
                "--output",
                file.path().to_str().ok_or("invalid temporary path")?,
                url,
            ],
            false,
        )?;
        let bytes = std::fs::read(file.path()).map_err(|_| "download unreadable")?;
        if !validate_script(&bytes) {
            return Err("download refused".into());
        }
        self.execute(
            "bash",
            &[file.path().to_str().ok_or("invalid temporary path")?],
            privileged,
        )?;
        if url == "https://deb.nodesource.com/setup_lts.x" {
            self.apt(&["nodejs"])?;
        }
        Ok(())
    }
}
pub fn cli(home: &Path, args: &[String]) -> i32 {
    let mut only = None;
    let mut dry_run = false;
    let mut check = false;
    let mut yes = false;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--only" if index + 1 < args.len() => {
                index += 1;
                only = Some(args[index].as_str());
            }
            "--dry-run" => dry_run = true,
            "--check" => check = true,
            "--yes" => yes = true,
            _ => return 2,
        }
        index += 1;
    }
    if !cfg!(target_os = "linux") {
        eprintln!("install requires Linux or WSL2; use the WSL bootstrap on Windows");
        return 2;
    }
    if !dry_run && !check && !yes {
        eprintln!("install refused: --yes required for system changes; preview with --dry-run");
        return 2;
    }
    if select(only).is_err() {
        eprintln!("install refused unknown component");
        return 2;
    }
    let _lock = if !dry_run && !check {
        let state = crate::paths::state(home);
        if std::fs::create_dir_all(&state).is_err() {
            return 2;
        }
        match crate::atomic::lock(&state.join("installer")) {
            Ok(lock) => Some(lock),
            Err(_) => return 2,
        }
    } else {
        None
    };
    #[cfg(unix)]
    let root = unsafe { libc::geteuid() == 0 }; // SAFETY: read-only libc identity query.
    #[cfg(not(unix))]
    let root = false;
    let mut host = SystemHost {
        home: home.into(),
        root,
        apt_updated: false,
    };
    let (code, rows) = run(&mut host, only, dry_run, check);
    for row in rows {
        println!("{row}");
    }
    code
}
