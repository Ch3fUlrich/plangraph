//! Coordinator command and host-local checks for one worker rejoin attempt.

use pg_worker_core::{
    disk_runtime::disk_usage,
    host_command,
    hosts::{HostEntry, HostRegistry},
    run::record::valid_run_id,
    worker_host::{Step, Store},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    env,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub(crate) fn now() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .map_err(|_| "system clock predates epoch".into())
}

pub(crate) fn registry() -> Result<HostRegistry, String> {
    let config = env::var_os("XDG_CONFIG_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))
        .ok_or("cannot resolve fleet configuration directory")?;
    HostRegistry::load(&config.join("plangraph/hosts.json"))
        .map_err(|_| "fleet host registry unavailable or invalid".into())
}

pub(crate) fn remote(host: &HostEntry) -> Result<Command, String> {
    let route = host.ssh_route().ok_or("worker has no SSH route")?;
    let mut command = host_command::ssh(&route).map_err(|_| "worker SSH route is invalid")?;
    if let Some(distro) = host.wsl_distro.as_deref() {
        if !host_command::valid_wsl_distro(distro) {
            return Err("invalid worker WSL distribution".into());
        }
        command.args(["wsl.exe", "--distribution", distro, "--exec"]);
    }
    Ok(command)
}

pub(crate) fn active_run_ids(
    state: &Path,
    registry: &HostRegistry,
    host: &str,
) -> BTreeSet<String> {
    crate::records::list_runs(state)
        .into_iter()
        .filter(|row| {
            matches!(
                row["status"].as_str(),
                Some("running" | "queued" | "unknown")
            )
        })
        .filter(|row| {
            row["host"]
                .as_str()
                .and_then(|name| registry.resolve(name))
                .is_some_and(|entry| entry.id == host)
        })
        .filter_map(|row| {
            row["run_id"]
                .as_str()
                .filter(|id| valid_run_id(id))
                .map(str::to_owned)
        })
        .collect()
}

fn output(command: &mut Command, seconds: u64, operation: &str) -> Result<Vec<u8>, String> {
    host_command::output(command, Duration::from_secs(seconds))
        .map_err(|_| format!("{operation} failed or timed out"))
}

const SYNC: [&str; 4] = [
    "systemctl",
    "--user",
    "start",
    "plangraph-host-sync.service",
];
const SYNC_TIMEOUT: u64 = 3600;

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let [name] = args else {
        return Err("usage: pg-spawnd rejoin HOST".into());
    };
    let registry = registry()?;
    let host = registry.resolve(name).ok_or("unknown worker host")?;
    if host.inventory_only() {
        return Err("worker rejoin requires an active worker configuration".into());
    }
    let local = host.id == registry.self_host().id;
    let state = crate::state::resolve_state(None, |key| env::var_os(key))?;
    let store = Store::new(&state, &host.id).map_err(|e| e.to_string())?;
    store
        .rejoin(now()?, |step| {
            if step == Step::Sync {
                let mut cmd = if local {
                    let mut cmd = Command::new(SYNC[0]);
                    cmd.args(&SYNC[1..]);
                    cmd
                } else {
                    let mut cmd = remote(host)?;
                    cmd.args(SYNC);
                    cmd
                };
                output(&mut cmd, SYNC_TIMEOUT + 10, "host sync service")?;
            }
            let key = serde_json::to_value(step).map_err(|error| error.to_string())?;
            let key = key.as_str().ok_or("invalid rejoin step")?;
            let mut args = vec![host.id.clone(), key.into()];
            if step == Step::Runs {
                let mut runs = store.unknown_runs().map_err(|error| error.to_string())?;
                runs.extend(active_run_ids(&state, &registry, &host.id));
                args.extend(runs);
            }
            let bytes = if local {
                let mut bytes = Vec::new();
                local_step(&args, &mut bytes)?;
                bytes
            } else {
                let mut cmd = remote(host)?;
                cmd.args(["pg-spawnd", "rejoin-step"]).args(args);
                output(&mut cmd, 600, key)?
            };
            let receipt: Value =
                serde_json::from_slice(&bytes).map_err(|_| format!("{key} receipt invalid"))?;
            if receipt["host"] != host.id || receipt["step"] != key || receipt["ok"] != true {
                return Err(format!(
                    "{key} receipt refused: {}",
                    receipt["reason"]
                        .as_str()
                        .unwrap_or("identity or result mismatch")
                ));
            }
            Ok(receipt["evidence"].clone())
        })
        .map_err(|e| e.to_string())?;
    writeln!(out, "{}", json!({"host":host.id,"phase":"ready"})).map_err(|e| e.to_string())
}

fn git(repo: &Path, args: &[&str], operation: &str) -> Result<String, String> {
    let bytes = output(
        Command::new("git").arg("-C").arg(repo).args(args),
        30,
        operation,
    )?;
    String::from_utf8(bytes)
        .map(|s| s.trim_end().into())
        .map_err(|_| format!("{operation} returned invalid output"))
}

fn checked_main(repo: &Path) -> Result<String, String> {
    let branch = git(
        repo,
        &["symbolic-ref", "--short", "HEAD"],
        "checkout branch",
    )?;
    let main = git(
        repo,
        &["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
        "origin/main",
    )?;
    let head = git(
        repo,
        &["rev-parse", "--verify", "HEAD^{commit}"],
        "checkout head",
    )?;
    let changes = git(
        repo,
        &["status", "--porcelain", "--untracked-files=no"],
        "checkout status",
    )?;
    if branch != "main" || head != main || !changes.is_empty() {
        return Err("checkout is not clean main at origin/main".into());
    }
    Ok(main)
}

fn install(host: &HostEntry, repo: &Path, main: &str) -> Result<(), String> {
    let hooks = git(
        repo,
        &["rev-parse", "--path-format=absolute", "--git-path", "hooks"],
        "effective hooks path",
    )?;
    let hooks = Path::new(&hooks)
        .canonicalize()
        .map_err(|_| "effective hooks directory unavailable")?;
    let hooks_repo = git(&hooks, &["rev-parse", "--show-toplevel"], "hooks checkout")?;
    if hooks
        != Path::new(&hooks_repo)
            .join("tools/hooks")
            .canonicalize()
            .map_err(|_| "tracked hooks unavailable")?
        || checked_main(Path::new(&hooks_repo))? != main
    {
        return Err("effective hooks differ from origin/main".into());
    }

    let expected = config_path(host, "PG_REJOIN_BUILD_RECEIPT")?;
    let expected = crate::records::read_record_file(&expected)
        .map_err(|_| "main build receipt unavailable")?;
    if expected["main_sha"] != main {
        return Err("main build receipt is stale".into());
    }
    let expected_sum = expected["main_sha256"]
        .as_str()
        .ok_or("main build checksum missing")?;
    let binary = env::current_exe().map_err(|_| "installed spawner path unavailable")?;
    let stamp = config_path(host, "PG_REJOIN_SOURCE_STAMP")?;
    let mut receipt = Vec::new();
    crate::install_check::execute(
        vec![
            "--host-id".into(),
            host.id.clone(),
            "--binary".into(),
            binary.to_str().ok_or("invalid binary path")?.into(),
            "--source-stamp".into(),
            stamp.to_str().ok_or("invalid source stamp path")?.into(),
            "--main-sha".into(),
            main.into(),
            "--main-sha256".into(),
            expected_sum.into(),
        ],
        &mut receipt,
    )?;
    let receipt: Value = serde_json::from_slice(&receipt).map_err(|_| "install receipt invalid")?;
    if receipt["status"] == "current" {
        Ok(())
    } else {
        Err(format!(
            "spawner install {} ({})",
            receipt["status"], receipt["reason"]
        ))
    }
}

fn config_path(host: &HostEntry, key: &str) -> Result<PathBuf, String> {
    host.env
        .get(key)
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or_else(|| format!("{key} requires an absolute path in the fleet host entry"))
}

pub(crate) fn local_step(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let [name, key, runs @ ..] = args else {
        return Err("usage: pg-spawnd rejoin-step HOST STEP".into());
    };
    let registry = registry()?;
    let host = registry.self_host();
    if &host.id != name {
        return Err("rejoin step reached a different host".into());
    }
    if host.inventory_only() {
        return Err("worker rejoin requires an active worker configuration".into());
    }
    let step = serde_json::from_value::<Step>(json!(key)).map_err(|_| "invalid rejoin step")?;
    if (!runs.is_empty() && step != Step::Runs) || runs.iter().any(|id| !valid_run_id(id)) {
        return Err("invalid rejoin run arguments".into());
    }
    let result = if step == Step::Runs {
        reconcile_runs(host, runs)
    } else {
        check_local(host, step).map(|()| Value::Null)
    };
    let receipt = match result {
        Ok(evidence) => json!({"host":host.id,"step":key,"ok":true,"evidence":evidence}),
        Err(reason) => json!({"host":host.id,"step":key,"ok":false,"reason":reason}),
    };
    writeln!(out, "{}", receipt).map_err(|e| e.to_string())
}

fn reconcile_runs(host: &HostEntry, runs: &[String]) -> Result<Value, String> {
    let reconciler = config_path(host, "PG_REJOIN_RUN_RECONCILER")?;
    let bytes = output(
        Command::new(reconciler)
            .arg(&host.id)
            .args(runs)
            .env("PLANGRAPH_SPAWN_STATE", &host.state_dir),
        540,
        "worker run reconciliation",
    )?;
    let receipt: Value =
        serde_json::from_slice(&bytes).map_err(|_| "invalid run reconciliation receipt")?;
    if receipt["host"] != host.id {
        return Err("run reconciliation host mismatch".into());
    }
    let rows = receipt["runs"]
        .as_array()
        .ok_or("missing reconciled runs")?;
    let mut evidence = Vec::new();
    let mut settled = BTreeSet::new();
    for row in rows {
        let id = row["run_id"]
            .as_str()
            .filter(|id| valid_run_id(id))
            .ok_or("invalid reconciled run id")?;
        let outcome = row["outcome"]
            .as_str()
            .filter(|value| matches!(*value, "collected" | "reattached" | "released"))
            .ok_or("run reconciliation is unresolved")?;
        if !settled.insert(id) {
            return Err("duplicate reconciled run id".into());
        }
        evidence.push(json!({"run_id":id, "outcome":outcome}));
    }
    if let Some(id) = runs.iter().find(|id| !settled.contains(id.as_str())) {
        return Err(format!("missing reconciliation for {id}"));
    }
    Ok(json!(evidence))
}

fn check_local(host: &HostEntry, step: Step) -> Result<(), String> {
    let config = host
        .repo("plangraph")
        .ok_or("worker has no plangraph checkout")?;
    let repo = Path::new(&config.root);
    match step {
        Step::Sync => {
            let main = checked_main(repo)?;
            let upstream = git(
                repo,
                &["ls-remote", "--exit-code", "origin", "refs/heads/main"],
                "live origin main",
            )?;
            if upstream != format!("{main}\trefs/heads/main") {
                return Err("host sync did not reach live origin/main".into());
            }
            Ok(())
        }
        Step::Install => install(host, repo, &checked_main(repo)?),
        Step::Runs => unreachable!("run reconciliation returns a structured receipt"),
        Step::Worktrees => {
            output(
                Command::new("pg-worktree-sweep")
                    .args([
                        "--apply",
                        "--repo",
                        &config.root,
                        "--root",
                        &config.worktrees_root,
                    ])
                    .env("PLANGRAPH_SPAWN_STATE", &host.state_dir),
                540,
                "worktree sweep",
            )?;
            output(
                Command::new("pg-worktree-sweep").arg("--check-units"),
                30,
                "unit worktree check",
            )
            .map(drop)
        }
        Step::Capacity => {
            for path in [
                repo,
                Path::new(&config.worktrees_root),
                Path::new(&host.state_dir),
            ] {
                if disk_usage(path).map_err(|_| "disk capacity unavailable")?
                    < 5 * 1024 * 1024 * 1024
                {
                    return Err("worker disk capacity below 5 GiB".into());
                }
            }
            if pg_worker_core::host_probe::probe(&host.id)
                .map_err(|_| "memory capacity unavailable")?
                .mem_available_mb
                < host.caps.mem_floor_mb
            {
                return Err("worker memory capacity below registry floor".into());
            }
            Ok(())
        }
        Step::Legs => output(
            Command::new(config.python.as_deref().unwrap_or("python3"))
                .args(["-m", "plangraph.spawner.doctor", "--probe", "--json"])
                .current_dir(repo)
                .env("PYTHONPATH", repo.join("src"))
                .env("PLANGRAPH_SPAWN_STATE", &host.state_dir),
            540,
            "routed leg probes",
        )
        .map(drop),
    }
}
