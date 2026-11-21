use serde_json::{json, Value};
use std::env;
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
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
}

impl Launcher {
    pub fn find() -> Option<Self> {
        if let Some(path) = env::var_os("PLANGRAPH_FLEET_LAUNCH") {
            let path = fs::canonicalize(path).expect("PLANGRAPH_FLEET_LAUNCH must exist");
            assert!(path.is_file(), "PLANGRAPH_FLEET_LAUNCH must name a file");
            return Some(Self::Explicit(path));
        }
        let checkout = PathBuf::from(env::var_os("HOME")?).join("code/plangraph-data");
        checkout
            .join("router/bin/fleet-launch")
            .is_file()
            .then_some(Self::Pinned(checkout))
    }

    fn install(&self, root: &Path) -> PathBuf {
        let checkout = match self {
            Self::Explicit(path) => return path.clone(),
            Self::Pinned(checkout) => checkout,
        };
        let router = root.join("router");
        for dir in ["bin", "ops"] {
            fs::create_dir_all(router.join(dir)).unwrap();
        }
        for file in [
            "bin/fleet-launch",
            "bin/fleet_hosts.py",
            "bin/fleet_launch_admission.py",
            "bin/fleet_launch_mem.py",
            "ops/fleet-launch-policy.json",
            "ops/fleet-settings.json",
            "ops/terse-style.txt",
        ] {
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
            fs::write(router.join(file), out.stdout).unwrap();
        }
        let path = router.join("bin/fleet-launch");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }
}

pub struct Sandbox {
    dir: TempDir,
    pub launcher: PathBuf,
}

impl Sandbox {
    pub fn new(launcher: &Launcher) -> Self {
        let dir = tempfile::Builder::new()
            .prefix("fleet-launch-parity-")
            .tempdir_in("/tmp")
            .unwrap();
        let launcher = launcher.install(dir.path());
        let fixture = Self { dir, launcher };
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
        cmd.args(["--kill-after=2s", "30s"]).arg(&self.launcher);
        self.isolate(&mut cmd);
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
            .env("FIXTURE_ROOT", self.dir.path());
    }

    pub fn launch(&self, role: &str) -> Command {
        let mut cmd = self.command();
        cmd.arg(role)
            .arg(self.path("work"))
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
        cmd
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
