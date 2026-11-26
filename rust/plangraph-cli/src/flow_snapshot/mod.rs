//! Run one read-only collection and atomically publish the watchdog snapshot.
mod live;

type Env = BTreeMap<String, String>;

use pg_worker_core::{fleet_series, flow_collect::assemble, hosts::HostRegistry};
use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::{
    io,
    path::{Path, PathBuf},
};

const USAGE: &str = "usage: plangraph flow-snapshot [--json] [--out PATH] [--readings FILE]";

#[derive(Default)]
struct Options {
    json: bool,
    out: Option<PathBuf>,
    readings: Option<PathBuf>,
}

fn options(args: &[String]) -> Result<Options, String> {
    let mut parsed = Options::default();
    let mut input = args.iter();
    while let Some(arg) = input.next() {
        match arg.as_str() {
            "--json" if !parsed.json => parsed.json = true,
            "--out" if parsed.out.is_none() => parsed.out = Some(input.next().ok_or(USAGE)?.into()),
            "--readings" if parsed.readings.is_none() => {
                parsed.readings = Some(input.next().ok_or(USAGE)?.into())
            }
            _ => return Err(USAGE.into()),
        }
    }
    Ok(parsed)
}

fn fixture(path: &Path) -> Result<Value, String> {
    let raw = std::fs::read(path).map_err(|error| format!("readings: {error}"))?;
    serde_json::from_slice(&raw).map_err(|error| format!("readings: {error}"))
}

fn write(path: PathBuf, snapshot: &[u8]) -> io::Result<()> {
    // A relative single-component name has no nonempty parent in std::path.
    let target = if path.is_absolute() {
        path
    } else {
        std::env::current_dir()?.join(path)
    };
    let parent = target
        .parent()
        .ok_or_else(|| io::Error::other("missing output directory"))?;
    std::fs::create_dir_all(parent)?;
    let _lock = crate::atomic::lock(&target)?;
    crate::atomic::write(&target, snapshot)
}

fn run(args: &[String]) -> Result<i32, String> {
    let options = options(args)?;
    let started = now()?;
    let base = crate::env::default_path()?;
    let env = crate::env::load(&base, std::env::vars())?;
    let readings = match &options.readings {
        Some(path) => fixture(path)?,
        None => live::collect(&env, started),
    };
    let result = assemble(&readings)?;
    let unavailable = &result.snapshot["unavailable_inputs"];
    let elapsed = now()?.saturating_sub(started);
    if !result.required_errors.is_empty() {
        eprintln!(
            "{}",
            json!({"command":"flow-snapshot","started_at":started,
            "elapsed_seconds":elapsed,"outcome":"required_input_failed",
            "required_input_errors":result.required_errors,"unavailable_inputs":unavailable})
        );
        return Ok(3);
    }
    let text = result.snapshot.to_string();
    let out = match options.out {
        Some(out) => out,
        None => state_dir(&env)?.join("flow-snapshot.json"),
    };
    write(out, text.as_bytes()).map_err(|error| format!("publish: {error}"))?;
    eprintln!(
        "{}",
        json!({"command":"flow-snapshot","started_at":started,
        "elapsed_seconds":now()?.saturating_sub(started),"outcome":"published",
        "unavailable_inputs":unavailable})
    );
    if options.json {
        println!("{text}");
    }
    Ok(0)
}

/// Exit 3 signals unavailable required evidence. Exit 2 signals an invocation or output error.
pub fn cli(args: &[String]) -> i32 {
    if matches!(args, [arg] if matches!(arg.as_str(), "--help" | "-h")) {
        println!("{USAGE}");
        return 0;
    }
    run(args).unwrap_or_else(|error| {
        eprintln!(
            "{}",
            json!({"command":"flow-snapshot","outcome":"error","reason":error})
        );
        2
    })
}

fn home(env: &Env) -> Result<PathBuf, String> {
    env.get("HOME")
        .or_else(|| env.get("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| "home unavailable".into())
}

pub(super) fn state_dir(env: &Env) -> Result<PathBuf, String> {
    if let Some(path) = env.get("PLANGRAPH_STATE_DIR") {
        return Ok(path.into());
    }
    Ok(env
        .get("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or(home(env)?.join(".local/state"))
        .join("plangraph"))
}

fn config_dir(env: &Env) -> Result<PathBuf, String> {
    Ok(env
        .get("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or(home(env)?.join(".config"))
        .join("plangraph"))
}

pub(super) fn now() -> Result<u64, String> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|_| "system clock predates Unix epoch".into())
}

pub(super) fn list(env: &Env, key: &str) -> BTreeSet<String> {
    env.get(key)
        .into_iter()
        .flat_map(|text| text.split(','))
        .map(str::trim)
        .filter(|word| !word.is_empty())
        .map(str::to_owned)
        .collect()
}

fn host_id(env: &Env) -> Result<String, String> {
    let host = match env
        .get("PLANGRAPH_HOST_NAME")
        .or_else(|| env.get("PLANGRAPH_HOST"))
    {
        Some(host) => host.clone(),
        None => {
            HostRegistry::load(&config_dir(env)?.join("hosts.json"))
                .map_err(|error| error.to_string())?
                .self_host
        }
    };
    if !fleet_series::valid_host(&host) {
        return Err("configured local host identity is invalid".into());
    }
    Ok(host)
}

fn leg_state(env: &BTreeMap<String, String>, started: u64) -> Result<Value, String> {
    let legs = list(env, "PLANGRAPH_FLOW_LEGS");
    if legs.is_empty() {
        return Err("PLANGRAPH_FLOW_LEGS has no configured leg keys".into());
    }
    // The existing leg-state store supplies exact scoped events. Keep raw observations,
    // leaving provider availability and cooldown interpretation to the routing reader.
    let path = env
        .get("PLANGRAPH_FLOW_LEG_STATE_DB")
        .map(PathBuf::from)
        .unwrap_or(state_dir(env)?.join("leg-state.sqlite"));
    let db = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| "leg-state store unavailable for read")?;
    let mut reader = db
        .prepare(
            "SELECT leg_key, account, combo, ts, status, reset_at, source
         FROM leg_events WHERE leg_key=?1 AND ts BETWEEN ?2 AND ?3
         ORDER BY ts DESC, id DESC LIMIT 1000",
        )
        .map_err(|_| "leg-state observation query unavailable")?;
    let mut output = Vec::new();
    for leg in legs {
        let mut rows = reader
            .query((
                leg,
                started.saturating_sub(pg_worker_core::flow_collect::SAMPLE_MAX_AGE_S),
                started,
            ))
            .map_err(|_| "leg-state observations unavailable")?;
        let mut scopes = BTreeSet::new();
        let mut count = 0;
        while let Some(row) = rows
            .next()
            .map_err(|_| "leg-state observation unreadable")?
        {
            count += 1;
            let row: rusqlite::Result<Value> = (|| {
                let account: Option<String> = row.get(1)?;
                let combo: Option<String> = row.get(2)?;
                if !scopes.insert((account.clone(), combo.clone())) {
                    return Ok(Value::Null);
                }
                Ok(
                    json!({"leg":row.get::<_, String>(0)?,"account":account,"combo":combo,
                    "observed_at":row.get::<_, f64>(3)?,"status":row.get::<_, u16>(4)?,
                    "reset_at":row.get::<_, Option<f64>>(5)?,"source":row.get::<_, String>(6)?}),
                )
            })();
            let observation = row.map_err(|_| "malformed leg-state observation")?;
            if !observation.is_null() {
                output.push(observation);
            }
        }
        if count == 0 || count == 1000 {
            return Err("leg-state observations are missing or hit their read limit".into());
        }
    }
    Ok(json!(output))
}
