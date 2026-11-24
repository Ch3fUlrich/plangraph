use serde_json::{json, Value};
use std::env;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use tempfile::TempDir;

pub const PIN: &str = "2b5a8c90725e2fdf4d5d5671c3e4589f5e03a5bf";
pub const LOCAL: &str = "fixture-local";
pub const REMOTE: &str = "fixture-remote";
pub const PROMPT: &str = "Fixture prompt";
pub const SID: &str = "12345678-1234-1234-1234-123456789abc";

pub enum Launcher {
    Explicit(PathBuf),
    Pinned(PathBuf),
    Rust(Option<PathBuf>),
}

impl Launcher {
    pub fn find() -> Option<Self> {
        if let Some(path) = env::var_os("PLANGRAPH_FLEET_LAUNCH") {
            let path = fs::canonicalize(path).expect("PLANGRAPH_FLEET_LAUNCH must exist");
            assert!(path.is_file(), "PLANGRAPH_FLEET_LAUNCH must name a file");
            return Some(Self::Explicit(path));
        }
        reference_checkout().map(Self::Pinned)
    }

    fn install(&self, root: &Path) -> PathBuf {
        let checkout = match self {
            Self::Explicit(path) => return path.clone(),
            Self::Pinned(checkout) | Self::Rust(Some(checkout)) => Some(checkout),
            Self::Rust(None) => None,
        };
        let router = root.join("router");
        for dir in ["bin", "ops"] {
            fs::create_dir_all(router.join(dir)).unwrap();
        }
        if let Some(checkout) = checkout {
            for file in [
                "bin/fleet-launch",
                "bin/fleet_hosts.py",
                "bin/fleet_launch_admission.py",
                "bin/fleet_launch_mem.py",
                "ops/fleet-launch-policy.json",
                "ops/fleet-settings.json",
                "ops/terse-style.txt",
            ] {
                install_pinned_file(checkout, &router, file);
            }
        } else {
            fs::write(
                router.join("ops/fleet-launch-policy.json"),
                include_str!("../fixtures/fleet-launch/ops-policy.json"),
            )
            .unwrap();
            fs::write(
                router.join("ops/fleet-settings.json"),
                include_str!("../fixtures/fleet-launch/ops-settings.json"),
            )
            .unwrap();
            fs::write(router.join("ops/terse-style.txt"), "x".repeat(881) + "\n").unwrap();
        }
        router.join("bin/fleet-launch")
    }
}

pub fn install_pinned_file(checkout: &Path, router: &Path, file: &str) {
    let object = format!("{PIN}:router/{file}");
    let out = Command::new("git")
        .arg("-C")
        .arg(checkout)
        .args(["show", &object])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "reading pinned {object}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    if file == "bin/fleet-launch" {
        pg_worker_core::executable_test_support::write_private_executable(
            router.join(file),
            out.stdout,
        );
    } else {
        fs::write(router.join(file), out.stdout).unwrap();
    }
}

pub fn reference_checkout() -> Option<PathBuf> {
    let code = PathBuf::from(env::var_os("HOME")?).join("code");
    let mut candidates: Vec<_> = fs::read_dir(code)
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.join("router/bin/fleet-launch").is_file())
        .collect();
    candidates.sort();
    candidates.into_iter().find(|path| {
        Command::new("git")
            .arg("-C")
            .arg(path)
            .args(["cat-file", "-e", &format!("{PIN}:router/bin/fleet-launch")])
            .output()
            .is_ok_and(|out| out.status.success())
    })
}

pub struct Sandbox {
    dir: TempDir,
    pub launcher: PathBuf,
    remote_router: PathBuf,
    rust: bool,
}

impl Sandbox {
    pub fn new(launcher: &Launcher) -> Self {
        let dir = tempfile::Builder::new()
            .prefix("fleet-launch-parity-")
            .tempdir_in("/tmp")
            .unwrap();
        let rust = matches!(launcher, Launcher::Rust(_));
        let launcher = launcher.install(dir.path());
        let remote_router = fs::read_to_string(dir.path().join("router/bin/fleet_hosts.py"))
            .ok()
            .and_then(|host_source| receiver_router(&host_source))
            .unwrap_or_else(|| PathBuf::from("code/fixture-data/router"));
        let fixture = Self {
            dir,
            launcher,
            remote_router,
            rust,
        };
        for path in ["work", "bin", "home"] {
            fs::create_dir(fixture.path(path)).unwrap();
        }
        for name in ["claude", "ssh", "powershell.exe"] {
            symlink(
                env::current_exe().unwrap(),
                fixture.path(&format!("bin/{name}")),
            )
            .unwrap();
        }
        fs::write(fixture.path("prompt"), PROMPT).unwrap();
        fs::write(fixture.path("mcp.json"), "{}\n").unwrap();
        fixture.hosts(json!([{"name": LOCAL, "memory_floor_gib": 0}]));
        fixture
    }

    pub fn path(&self, path: &str) -> PathBuf {
        self.dir.path().join(path)
    }

    pub fn hosts(&self, entries: Value) {
        self.write_json(
            "hosts.json",
            json!({"hosts": entries, "worker_roles": ["writer"]}),
        );
    }

    pub fn write_json(&self, path: &str, data: Value) {
        let path = self.path(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, data.to_string()).unwrap();
    }

    pub fn json(&self, path: &str) -> Value {
        serde_json::from_str(&fs::read_to_string(self.path(path)).unwrap()).unwrap()
    }

    pub fn command(&self) -> Command {
        let mut cmd = Command::new("/usr/bin/timeout");
        cmd.args(["--kill-after=2s", "30s"]);
        self.configure_launcher(&mut cmd, self.rust);
        cmd
    }

    fn configure_launcher(&self, cmd: &mut Command, rust: bool) {
        if rust {
            cmd.args([env!("CARGO_BIN_EXE_pg-spawnd"), "session"]);
        } else {
            cmd.arg(&self.launcher);
        }
        self.isolate(cmd);
    }

    pub fn install_destination(&self, target: PathBuf) {
        if self.rust {
            symlink(env::current_exe().unwrap(), target).unwrap();
        } else {
            symlink(&self.launcher, target).unwrap();
        }
    }

    pub fn python_command(&self) -> Command {
        let mut cmd = Command::new("/usr/bin/timeout");
        self.isolate(&mut cmd);
        cmd.args(["--kill-after=2s", "30s"])
            .arg(super::test_python::command().get_program());
        cmd
    }

    pub fn isolate(&self, cmd: &mut Command) {
        cmd.current_dir(self.dir.path())
            .env_clear()
            .env("LC_ALL", "C")
            .env(
                "PATH",
                format!("{}:/usr/bin:/bin", self.path("bin").display()),
            )
            .env("HOME", self.path("home"))
            .env("ROUTING_STATE_DIR", self.path("state"))
            .env("XDG_STATE_HOME", self.path("xdg"))
            .env("OOM_STATE_DIR", self.path("oom"))
            .env("CTX_GUARD_SESSION_DIR", self.path("guard"))
            .env("FLEET_HOST_POLICY", self.path("hosts.json"))
            .env("FLEET_LOCAL_HOST", LOCAL)
            .env("FLEET_MCP_CONFIG", self.path("mcp.json"))
            .env("FLEET_RESOLVE_BUDGET", "0")
            .env("PLANGRAPH_FLEET_OPS", self.path("router/ops"))
            .env("PLANGRAPH_FLEET_REMOTE_ROUTER", &self.remote_router)
            .env("FIXTURE_ROOT", self.dir.path());
    }

    pub fn launch(&self, role: &str) -> Command {
        self.launch_at(role, "work")
    }

    /// Launch in another working directory below the sandbox root.
    pub fn launch_at(&self, role: &str, cwd: &str) -> Command {
        let mut cmd = self.command();
        self.launch_args(&mut cmd, role, cwd);
        cmd
    }

    pub fn direct_launch(&self, role: &str, rust: bool) -> Command {
        let mut cmd = if rust {
            let mut command = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
            command.arg("session");
            command
        } else {
            Command::new(&self.launcher)
        };
        self.isolate(&mut cmd);
        self.launch_args(&mut cmd, role, "work");
        cmd
    }

    fn launch_args(&self, cmd: &mut Command, role: &str, cwd: &str) {
        cmd.arg(role)
            .arg(self.path(cwd))
            .args(["sonnet", "high"])
            .arg(self.path("prompt"))
            .args([
                "--owner",
                "fixture",
                "--track",
                "parity",
                "--reason",
                "fixture",
                "--cap",
                "declared",
                "--handoff",
                "handoff.md",
            ]);
    }

    pub fn normalize(&self, bytes: &[u8]) -> String {
        String::from_utf8(bytes.to_vec())
            .unwrap()
            .replace(self.dir.path().to_str().unwrap(), "$ROOT")
    }

    pub fn expect(&self, out: &Output, code: i32, stdout: &str, stderr: &str) {
        let actual = (
            out.status.code(),
            self.normalize(&out.stdout),
            self.normalize(&out.stderr),
        );
        assert_eq!(actual, (Some(code), stdout.to_owned(), stderr.to_owned()));
    }

    pub fn no_launch(&self) {
        for path in ["child.json", "state", "oom", "guard", "xdg"] {
            assert!(!self.path(path).exists(), "{path} was written");
        }
    }
}

pub fn receiver_router(source: &str) -> Option<PathBuf> {
    let (_, suffix) = source.split_once("root=pathlib.Path.home()/\"")?;
    let (path, _) = suffix.split_once('"')?;
    Some(PathBuf::from(path))
}
