//! Read-only adapters used by the CLI. A source failure never erases another source's facts.
use crate::{
    ci_watch::github::{self, Gh, Github},
    deploy_manifest::{self, Manifest, Scope},
    keeper_events::Client,
    land::{
        git,
        main_gh::{self, GhSource},
        main_state::Source,
    },
    runner::download::sha256_file,
};
use chrono::{DateTime, Utc};
use pg_worker_core::{disk_runtime::disk_space, fleet_series, flow_collect::CI_WINDOW_S};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

use super::{config_dir, host_id, list, now, state_dir, Env};
type Fields = Map<String, Value>;
type Reading = Result<Value, String>;
pub(super) const READY: &str = "v1/ready?fmt=json&agent=true&all=1";
pub(super) const SNAPSHOT: &str = "v1/snapshot?fmt=json";

fn root(env: &Env) -> Result<PathBuf, String> {
    match env.get("PLANGRAPH_ROOT") {
        Some(root) => Ok(root.into()),
        None => git::out(Path::new("."), &["rev-parse", "--show-toplevel"]).map(PathBuf::from),
    }
}

fn read_series(dir: &Path, hosts: BTreeSet<&String>) -> Reading {
    let mut rows = Vec::new();
    for host in hosts {
        if !fleet_series::valid_host(host) {
            return Err("invalid configured fleet host identity".into());
        }
        let history = fleet_series::read(&dir.join(format!("{host}.ndjson")))
            .map_err(|error| format!("series read for {host}: {error}"))?;
        let latest = history
            .last()
            .filter(|sample| &sample.host == host)
            .ok_or_else(|| format!("fleet history has no latest row for {host}"))?;
        rows.push(latest.clone());
    }
    Ok(json!(rows))
}

fn usage(env: &Env, root: &Path) -> Reading {
    let output = Command::new("python3")
        .envs(env)
        .arg(root.join("tools/claude_usage.py"))
        .args(["pace", "--json"])
        .output()
        .map_err(|error| format!("cannot run usage reader: {error}"))?;
    if !output.status.success() {
        return Err(format!("usage pace reader failed ({})", output.status));
    }
    let body: Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "usage pace reader did not return JSON")?;
    if !body["result"].is_object() {
        return Err("usage pace reader returned no result".into());
    }
    Ok(body["result"].clone())
}

fn main_source(root: &Path) -> Result<GhSource<Gh>, String> {
    let repo = github::repository(&git::out(root, &["remote", "get-url", "origin"])?)?;
    Ok(GhSource {
        gh: Gh { repo },
        branch: "main".into(),
        now: || Utc::now().timestamp(),
        land_app: main_gh::land_app_from_env(),
    })
}

fn protected_tip(source: &mut GhSource<Gh>) -> Result<String, String> {
    if !source.forward_only()? {
        return Err("origin main is not verified forward-only".into());
    }
    let sha = source.tip()?;
    if sha.len() != 40 || !sha.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("protected-main tip has no exact Git SHA".into());
    }
    Ok(sha)
}

#[derive(Deserialize)]
struct Run {
    id: u64,
    head_sha: String,
    head_branch: String,
    event: String,
    status: String,
    conclusion: Option<String>,
    created_at: String,
}

fn ci(source: &mut GhSource<Gh>, root: &Path, tip: &str, started: u64) -> Reading {
    let since = DateTime::from_timestamp(started.saturating_sub(CI_WINDOW_S) as i64, 0)
        .ok_or("no usable CI window start")?
        .format("%Y-%m-%dT%H:%M:%SZ");
    // The repository's test workflow on push to protected main binds the run to its own
    // head. A PR's head SHA alone would not prove which merge tree was actually tested.
    let query = format!(
        "actions/workflows/tests.yml/runs?branch=main&event=push&status=completed&created=%3E%3D{since}&per_page=100"
    );
    let rows = source.gh.pages(&query, Some("workflow_runs"))?;
    if rows.len() >= 1000 {
        return Err("GitHub CI window hit the filtered result limit".into());
    }
    let mut seen = BTreeSet::new();
    let mut ancestry = BTreeMap::new();
    let mut runs = Vec::new();
    for row in rows {
        let run: Run = serde_json::from_value(row).map_err(|_| "malformed CI run observation")?;
        if run.head_branch != "main"
            || run.event != "push"
            || run.status != "completed"
            || run.head_sha.len() != 40
            || !run.head_sha.bytes().all(|byte| byte.is_ascii_hexdigit())
            || !seen.insert(run.id)
        {
            return Err(
                "CI observation is repeated or outside the requested protected-main window".into(),
            );
        }
        let on_main = match ancestry.get(&run.head_sha) {
            Some(on_main) => *on_main,
            None => {
                let on_main = git::is_ancestor(root, &run.head_sha, tip)?;
                ancestry.insert(run.head_sha, on_main);
                on_main
            }
        };
        if !on_main {
            return Err("CI run head is not an ancestor of the observed protected-main tip".into());
        }
        let conclusion = run.conclusion.ok_or("completed CI run has no conclusion")?;
        runs.push(json!({"conclusion":conclusion,"created_at":run.created_at}));
    }
    Ok(json!(runs))
}

fn host_code(env: &Env, root: &Path, tip: &str, started: u64) -> Reading {
    let host = host_id(env)?;
    let checkout = git::out(root, &["rev-parse", "HEAD"])?;
    let range = format!("{checkout}..{tip}");
    let behind: u64 = git::out(root, &["rev-list", "--count", &range])?
        .parse()
        .map_err(|_| "invalid protected-main ancestry count")?;
    let stamps = git::out(
        root,
        &["log", "--first-parent", "--reverse", "--format=%ct", &range],
    )?;
    let age = match stamps.lines().next() {
        Some(first) => {
            started.saturating_sub(
                first
                    .parse()
                    .map_err(|_| "invalid first missing commit time")?,
            ) as f64
                / 3600.0
        }
        None => 0.0,
    };
    let tmp = env
        .get("PLANGRAPH_HOST_TMP_PATH")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let percent = |path: &Path| {
        let space =
            disk_space(path).map_err(|error| format!("disk sample unavailable: {error}"))?;
        if space.total == 0 {
            return Err("disk sample has zero total bytes".into());
        }
        Ok::<_, String>(100.0 * (1.0 - space.free as f64 / space.total as f64))
    };
    let mut row = json!({"host":host,"checkout_head":checkout,"origin_main_head":tip,
        "behind_commits":behind,"age_hours":age});
    match percent(&tmp).and_then(|tmp| percent(Path::new("/")).map(|root| (tmp, root))) {
        Ok((tmp, root)) => {
            row["tmp_used_percent"] = json!(tmp);
            row["root_used_percent"] = json!(root);
        }
        Err(reason) => row["disk_error"] = json!(reason),
    }
    Ok(json!([row]))
}

fn units(env: &Env, root: &Path) -> Reading {
    let config = config_dir(env)?;
    let roles_text = match fs::read_to_string(config.join("host-roles")) {
        Ok(text) => Some(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("unit roles unavailable: {error}")),
    };
    let roles = deploy_manifest::host_roles(
        env.get("PG_HOST_ROLES").map(String::as_str),
        roles_text.as_deref(),
    )?;
    let manifest = Manifest::load_from_repo(root)?;
    let user_dir =
        pg_worker_core::unit_refs::systemd_user_dir().map_err(|error| error.to_string())?;
    let mut rows = Vec::new();
    for unit in manifest
        .units_for(&roles)
        .into_iter()
        .filter(|unit| unit.installer.is_none())
    {
        let installed = match unit.scope {
            Scope::User => &user_dir,
            Scope::System => Path::new("/etc/systemd/system"),
        };
        let source = root
            .join("deploy")
            .join(unit.dir.as_deref().unwrap_or("systemd"));
        for name in &unit.files {
            let path = installed.join(name);
            let deployed = match fs::metadata(&path) {
                Ok(_) => Some(sha256_file(&path)?),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
                Err(error) => return Err(format!("installed unit {name} unreadable: {error}")),
            };
            rows.push(
                json!({"name":name,"expected_sha":sha256_file(&source.join(name))?,
                "deployed_sha":deployed}),
            );
        }
    }
    if rows.is_empty() {
        return Err("manifest has no directly comparable units for this host".into());
    }
    Ok(json!(rows))
}

fn put(readings: &mut Fields, failures: &mut Fields, name: &str, result: Reading) {
    match result {
        Ok(value) => {
            readings.insert(name.into(), value);
        }
        Err(reason) => {
            failures.insert(name.into(), json!(reason));
        }
    }
}

fn encode_query(key: &str) -> String {
    use std::fmt::Write;
    let mut escaped = String::new();
    for byte in key.bytes() {
        if byte.is_ascii_alphanumeric() || b":-._~".contains(&byte) {
            escaped.push(byte as char);
        } else {
            write!(escaped, "%{byte:02X}").expect("String write");
        }
    }
    escaped
}

fn read_keeper(
    mut read: impl FnMut(&str) -> Reading,
    readings: &mut Fields,
    failures: &mut Fields,
) {
    let snapshot = read(SNAPSHOT);
    if let Ok(snapshot) = &snapshot {
        let mut details = Fields::new();
        let mut seen = BTreeSet::new();
        for item in snapshot["items"].as_array().into_iter().flatten() {
            if !matches!(item["state"].as_str(), Some("claimed" | "in_review")) {
                continue;
            }
            let Some(key) = item["key"].as_str().filter(|key| seen.insert(*key)) else {
                continue;
            };
            let path = format!("v1/show?fmt=json&key={}", encode_query(key));
            put(&mut details, failures, key, read(&path));
        }
        readings.insert("item_details".into(), Value::Object(details));
    }
    put(readings, failures, "keeper", snapshot);
    put(readings, failures, "ready", read(READY));
}

pub(super) fn collect(env: &Env, started: u64) -> Value {
    let mut readings = Fields::new();
    let mut failures = Fields::new();
    match Client::new(env) {
        Ok(keeper) => read_keeper(|path| keeper.get(path), &mut readings, &mut failures),
        Err(reason) => read_keeper(|_| Err(reason.clone()), &mut readings, &mut failures),
    }
    let root = root(env);
    let at_root =
        |read: &dyn Fn(&Path) -> Reading| root.as_deref().map_err(Clone::clone).and_then(read);
    put(
        &mut readings,
        &mut failures,
        "usage",
        at_root(&|root| usage(env, root)),
    );
    put(
        &mut readings,
        &mut failures,
        "units",
        at_root(&|root| units(env, root)),
    );
    let mut protected = root.as_deref().map_err(Clone::clone).and_then(main_source);
    let tip = protected
        .as_mut()
        .map_err(|error| error.clone())
        .and_then(protected_tip);
    put(
        &mut readings,
        &mut failures,
        "hosts",
        at_root(&|root| host_code(env, root, tip.as_deref().map_err(Clone::clone)?, started)),
    );
    put(
        &mut readings,
        &mut failures,
        "ci",
        root.as_deref().map_err(Clone::clone).and_then(|root| {
            ci(
                protected.as_mut().map_err(|error| error.clone())?,
                root,
                tip.as_deref().map_err(Clone::clone)?,
                started,
            )
        }),
    );
    let coding = list(env, "PLANGRAPH_FLOW_CODING_HOSTS");
    let workers = list(env, "PLANGRAPH_FLOW_WORKER_HOSTS");
    let series_dir = env
        .get("PLANGRAPH_FLEET_SERIES_DIR")
        .map(PathBuf::from)
        .map_or_else(|| state_dir(env).map(|state| state.join("fleet")), Ok);
    put(
        &mut readings,
        &mut failures,
        "series",
        series_dir.and_then(|dir| read_series(&dir, coding.union(&workers).collect())),
    );
    readings.insert("coding_hosts".into(), json!(coding));
    readings.insert("worker_hosts".into(), json!(workers));
    put(
        &mut readings,
        &mut failures,
        "leg_state",
        super::leg_state(env, started),
    );
    let resources = env
        .get("PLANGRAPH_FLOW_HOST_IDLE_FILE")
        .ok_or_else(|| "typed host-idle resource file is not configured".to_owned())
        .and_then(|path| super::fixture(Path::new(path)));
    put(
        &mut readings,
        &mut failures,
        "host_idle_capacity",
        resources,
    );
    put(
        &mut readings,
        &mut failures,
        "critical_target",
        env.get("PLANGRAPH_FLOW_CRITICAL_TARGET")
            .map(|target| json!(target))
            .ok_or_else(|| "PLANGRAPH_FLOW_CRITICAL_TARGET is not configured".into()),
    );
    put(
        &mut readings,
        &mut failures,
        "now",
        now().map(|stamp| json!(stamp)),
    );
    readings.insert("source_errors".into(), Value::Object(failures));
    Value::Object(readings)
}

#[cfg(test)]
#[path = "live_tests.rs"]
mod tests;
