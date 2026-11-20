//! Model-free host admission. Never starts a thread/turn or changes Codex settings.
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    time::Duration,
};

pub trait Probe {
    fn check(&mut self, point: &'static str) -> Result<(), String>;
}
pub fn check(host: &mut impl Probe) -> Vec<String> {
    let mut rows = Vec::new();
    for point in [
        "version", "login", "prompts", "sandbox", "commit", "identity", "memory",
    ] {
        if let Err(reason) = host.check(point) {
            rows.push(format!("{point}: {}", reason.replace(['\n', '\r'], " ")));
        }
    }
    if rows.is_empty() {
        rows.push("READY".into());
    }
    rows
}

pub fn run(args: &[String]) -> i32 {
    let [repo, mode] = args else {
        eprintln!("usage: plangraph codex-host-readiness WORKTREE read-only|workspace-write|danger-full-access");
        return 2;
    };
    if !matches!(
        mode.as_str(),
        "read-only" | "workspace-write" | "danger-full-access"
    ) {
        eprintln!("unknown sandbox mode");
        return 2;
    }
    let repo = match Path::new(repo).canonicalize() {
        Ok(path) if path.is_dir() => path,
        _ => {
            eprintln!("worktree unavailable");
            return 2;
        }
    };
    let rows = check(&mut Host {
        repo,
        mode: mode.clone(),
    });
    let ready = rows == ["READY"];
    for row in rows {
        println!("{row}");
    }
    i32::from(!ready)
}

struct Host {
    repo: PathBuf,
    mode: String,
}
fn output(command: &mut Command) -> Result<String, String> {
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| e.to_string())?;
    let mut stdout = child.stdout.take().ok_or("child stdout unavailable")?;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .read_to_end(&mut bytes)
            .map(|_| String::from_utf8_lossy(&bytes).into_owned());
        let _ = tx.send(result);
    });
    let result = rx.recv_timeout(Duration::from_secs(10));
    if result.is_err() {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let result = result
        .map_err(|_| "probe exceeded 10 seconds")?
        .map_err(|e| e.to_string())?;
    if status.success() {
        Ok(result)
    } else {
        Err(format!("command failed ({status})"))
    }
}
impl Host {
    fn codex(&self) -> Command {
        let mut cmd = Command::new("codex");
        clean_git_env(&mut cmd);
        cmd.current_dir(&self.repo);
        cmd
    }
    fn sandbox_command(&self, cwd: &Path, program: &str, args: &[&str]) -> Command {
        let mut cmd = if self.mode == "danger-full-access" {
            let mut cmd = Command::new(program);
            clean_git_env(&mut cmd);
            cmd
        } else {
            let mut cmd = self.codex();
            cmd.args(["sandbox"]).args([
                "-c",
                &format!("sandbox_mode=\"{}\"", self.mode),
                "--",
                program,
            ]);
            cmd
        };
        cmd.current_dir(cwd).args(args);
        cmd
    }
    fn commit(&self) -> Result<(), String> {
        // A linked worktree is essential: its gitdir is not inside the checkout.
        // The scratch lives beside the worktree (same filesystem, never inside it, so a
        // killed probe cannot leave files a later `git add -A` would commit) and the
        // sandbox cwd is the scratch root, so both gitdir and checkout are writable roots.
        let parent = self
            .repo
            .parent()
            .ok_or("worktree has no parent directory")?;
        let scratch = tempfile::Builder::new()
            .prefix(".pg-readiness-")
            .tempdir_in(parent)
            .map_err(|e| e.to_string())?;
        let source = scratch.path().join("source");
        let work = scratch.path().join("work");
        output(scratch_git().args(["init", "--quiet"]).arg(&source))?;
        output(scratch_git().current_dir(&source).args([
            "commit",
            "--allow-empty",
            "-m",
            "readiness baseline",
        ]))?;
        output(
            scratch_git()
                .current_dir(&source)
                .args(["worktree", "add", "--detach"])
                .arg(&work),
        )?;
        let work_arg = work.to_string_lossy();
        let mut cmd = self.sandbox_command(
            scratch.path(),
            "git",
            &[
                "-C",
                &work_arg,
                "commit",
                "--allow-empty",
                "-m",
                "readiness probe",
            ],
        );
        identity(&mut cmd);
        output(&mut cmd)?;
        let count = output(
            scratch_git()
                .current_dir(&work)
                .args(["rev-list", "--count", "HEAD"]),
        )?;
        if count.trim() != "2" {
            return Err("scratch commit did not advance HEAD".into());
        }
        Ok(())
    }
    fn prompts(&self) -> Result<(), String> {
        // Ask Codex to resolve layered configuration and hook trust itself.
        // This RPC surface only reads config/metadata, never submits a model request.
        let mut child = self
            .codex()
            .arg("app-server")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| e.to_string())?;
        let mut input = child.stdin.take().ok_or("app-server stdin unavailable")?;
        let mut lines =
            BufReader::new(child.stdout.take().ok_or("app-server stdout unavailable")?).lines();
        let repo = self.repo.clone();
        let trust_root = output(git().current_dir(&repo).args([
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ]))
        .ok()
        .map(|s| PathBuf::from(s.trim()))
        .and_then(|p| {
            (p.file_name()? == ".git")
                .then(|| p.parent().map(Path::to_path_buf))
                .flatten()
        });
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let result = (|| {
                rpc(
                    &mut input,
                    &mut lines,
                    1,
                    "initialize",
                    json!({"clientInfo":{"name":"pg-readiness","version":"1"},"capabilities":{"experimentalApi":true}}),
                )?;
                writeln!(input, "{}", json!({"method":"initialized"}))
                    .map_err(|e| e.to_string())?;
                let config = rpc(
                    &mut input,
                    &mut lines,
                    2,
                    "config/read",
                    json!({"cwd":repo,"includeLayers":false}),
                )?;
                let hooks = rpc(
                    &mut input,
                    &mut lines,
                    3,
                    "hooks/list",
                    json!({"cwds":[repo]}),
                )?;
                prompt_state(&config["config"], &hooks, &repo, trust_root.as_deref())
            })();
            let _ = tx.send(result);
        });
        let result = rx
            .recv_timeout(Duration::from_secs(10))
            .unwrap_or_else(|_| Err("config/hooks probe exceeded 10 seconds".into()));
        // No daemon or background process survives the check, including on RPC errors.
        let _ = child.kill();
        let _ = child.wait();
        result
    }
}
/// Why a git identity cannot author the writer's commits (a placeholder or an unset
/// value), or None when it is usable. The review exclusion and the commit gate read these
/// commits, so a placeholder such as `x <x@example.invalid>` must not reach them.
pub fn identity_problem(name: &str, email: &str) -> Option<String> {
    let (name, email) = (name.trim(), email.trim().to_ascii_lowercase());
    let domain = email.rsplit_once('@').map_or("", |(_, domain)| domain);
    if name.is_empty() || domain.is_empty() {
        return Some("git user.name and user.email must both be set".into());
    }
    let placeholder = domain == "localhost"
        || domain.ends_with(".invalid")
        || domain.ends_with(".test")
        || domain.ends_with(".localhost")
        || ["example.com", "example.org", "example.net"].contains(&domain);
    placeholder.then(|| format!("git identity {name} <{email}> is a placeholder"))
}
impl Host {
    fn identity(&self) -> Result<(), String> {
        let value = |key: &str| {
            output(git().current_dir(&self.repo).args(["config", "--get", key])).unwrap_or_default()
        };
        match identity_problem(&value("user.name"), &value("user.email")) {
            Some(problem) => Err(format!("{problem}; set user.name/user.email on this host")),
            None => Ok(()),
        }
    }
}
fn git() -> Command {
    let mut cmd = Command::new("git");
    clean_git_env(&mut cmd);
    cmd
}
fn scratch_git() -> Command {
    let mut cmd = git();
    identity(&mut cmd);
    cmd
}
fn clean_git_env(cmd: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(key);
        }
    }
}
fn identity(cmd: &mut Command) {
    // Identity and git config are isolated to the child: no global/system config or
    // core.hooksPath, so no hook runs and nothing is persisted.
    cmd.env("GIT_AUTHOR_NAME", "Readiness probe")
        .env("GIT_AUTHOR_EMAIL", "readiness@example.invalid")
        .env("GIT_COMMITTER_NAME", "Readiness probe")
        .env("GIT_COMMITTER_EMAIL", "readiness@example.invalid")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "core.hooksPath")
        .env("GIT_CONFIG_VALUE_0", "/dev/null");
}
fn rpc(
    input: &mut impl Write,
    lines: &mut impl Iterator<Item = std::io::Result<String>>,
    id: u64,
    method: &str,
    params: Value,
) -> Result<Value, String> {
    writeln!(
        input,
        "{}",
        json!({"id":id,"method":method,"params":params})
    )
    .map_err(|e| e.to_string())?;
    input.flush().map_err(|e| e.to_string())?;
    for line in lines {
        let value: Value = serde_json::from_str(&line.map_err(|e| e.to_string())?)
            .map_err(|_| "invalid app-server response")?;
        if value["id"] == id {
            return value
                .get("result")
                .cloned()
                .ok_or_else(|| format!("{method} unavailable; check Codex version/config"));
        }
    }
    Err(format!("{method}: app-server closed without response"))
}

pub fn prompt_state(
    config: &Value,
    hooks: &Value,
    repo: &Path,
    trust_root: Option<&Path>,
) -> Result<(), String> {
    let trusted = config["projects"].as_object().is_some_and(|projects| {
        // Exact cwd wins over the main repository trust root. Arbitrary ancestors do not confer trust.
        std::iter::once(repo)
            .chain(trust_root)
            .find_map(|p| projects.get(&p.to_string_lossy().into_owned()))
            .is_some_and(|v| v["trust_level"] == "trusted")
    });
    let mut missing = Vec::new();
    if !trusted {
        missing.push("folder trust unresolved");
    }
    if config["check_for_update_on_startup"] != false {
        missing.push("update prompt possible; centrally manage updates and disable startup checks");
    }
    let hook_ok = hooks["data"].as_array().is_some_and(|entries| {
        entries.len() == 1
            && entries.iter().all(|entry| {
                entry["errors"].as_array().is_some_and(Vec::is_empty)
                    && entry["warnings"].as_array().is_some_and(Vec::is_empty)
                    && entry["hooks"].as_array().is_some_and(|hooks| {
                        hooks.iter().all(|h| {
                            h["enabled"] == false
                                || matches!(h["trustStatus"].as_str(), Some("trusted" | "managed"))
                        })
                    })
            })
    });
    if !hook_ok {
        missing.push("hook trust unresolved; review /hooks");
    }
    if missing.is_empty() {
        Ok(())
    } else {
        Err(missing.join("; "))
    }
}
impl Probe for Host {
    fn check(&mut self, point: &'static str) -> Result<(), String> {
        match point {
            "version" => output(self.codex().arg("--version")).and_then(|s| {
                if s.trim().is_empty() { Err("Codex version unavailable".into()) } else { Ok(()) }
            }),
            "login" => output(self.codex().args(["login", "status"])).map(|_| ()).map_err(|_| "run codex login on this host".into()),
            "prompts" => self.prompts(),
            "sandbox" => output(&mut self.sandbox_command(&self.repo, "true", &[])).map(|_| ()).map_err(|_| "cannot start chosen sandbox (check bwrap/RTM_NEWADDR permissions)".into()),
            // A read-only sandbox cannot commit by design; nothing to probe there.
            "commit" if self.mode == "read-only" => Ok(()),
            "commit" => self.commit().map_err(|_| "scratch linked-worktree commit failed (check read-only git metadata, identity and hooks)".into()),
            // A read-only sandbox never commits, so its identity is never used.
            "identity" if self.mode == "read-only" => Ok(()),
            "identity" => self.identity(),
            "memory" => {
                let info = std::fs::read_to_string("/proc/meminfo").map_err(|_| "free memory unavailable (Linux required)")?;
                let kb = info.lines().find_map(|l| l.strip_prefix("MemAvailable:")?.split_whitespace().next()?.parse::<u64>().ok()).ok_or("MemAvailable unavailable")?;
                if kb < 4 * 1024 * 1024 { Err(format!("only {kb} KiB available; need 4 GiB")) } else { Ok(()) }
            }
            _ => Err("unknown check".into()),
        }
    }
}

#[cfg(test)]
#[path = "codex_readiness_tests.rs"]
mod tests;
