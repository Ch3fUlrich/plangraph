//! Default-off file-only integration. The caller supplies both clocks; persisted
//! epochs, detector and delivery records must never be reset on decode failure.
use crate::{fleet_stale_pair_alert as pair, fleet_stale_pair_delivery as delivery};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Clone, Default, Debug, Serialize, Deserialize)]
pub struct Clock {
    pub raw_ms: u64,
    pub total_ms: u64,
    pub wall_s: i64,
}
pub fn advance(saved: Option<&Clock>, raw_ms: u64, wall_s: i64, tolerance: u64) -> (Clock, bool) {
    let delta = saved.map_or(raw_ms, |s| {
        if raw_ms < s.raw_ms {
            raw_ms
        } else {
            raw_ms - s.raw_ms
        }
    });
    let jump = saved.is_some_and(|s| {
        ((wall_s as i128 - s.wall_s as i128) * 1000 - delta as i128).unsigned_abs()
            > tolerance as u128 * 1000
    });
    (
        Clock {
            raw_ms,
            total_ms: saved.map_or(delta, |s| s.total_ms.saturating_add(delta)),
            wall_s,
        },
        jump,
    )
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    schema_version: u8,
    #[serde(default)]
    enabled: bool,
    #[serde(default)]
    stale_pair: bool,
    state_path: Option<PathBuf>,
    outbox_path: Option<PathBuf>,
    acks_path: Option<PathBuf>,
    wall_jump_tolerance_s: Option<u64>,
    pairs: Option<Pairs>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Pairs {
    config: pair::Config,
    groups: Vec<Group>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Group {
    group: String,
    snapshot_path: PathBuf,
}
#[derive(Serialize, Deserialize)]
struct PairState {
    detector: pair::State,
    delivery: delivery::Delivery,
    active: Option<String>,
}
#[derive(Serialize, Deserialize)]
struct State {
    schema_version: u8,
    clock: Option<Clock>,
    pairs: BTreeMap<String, PairState>,
}
fn append(path: &Path, value: Value) -> Result<(), String> {
    let mut bytes = serde_json::to_vec(&value).map_err(|_| "outbox encoding")?;
    bytes.push(b'\n');
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|mut f| f.write_all(&bytes))
        .map_err(|_| "outbox append failed".into())
}
pub fn invoke(args: &[String], env: Option<PathBuf>, raw: u64, wall: i64) -> Result<Value, String> {
    let path = match args {
        [tick] if tick == "tick" => env,
        [tick, flag, path] if tick == "tick" && flag == "--config" => Some(path.into()),
        _ => return Err("invalid arguments".into()),
    };
    let Some(path) = path else {
        return Ok(json!({"enabled":false}));
    };
    let config: Config = serde_json::from_slice(&fs::read(path).map_err(|_| "config read failed")?)
        .map_err(|_| "invalid config")?;
    if config.schema_version != 1 {
        return Err("config schema".into());
    }
    if !config.enabled {
        return Ok(json!({"enabled":false}));
    }
    run(config, raw, wall)
}
fn run(c: Config, raw: u64, wall: i64) -> Result<Value, String> {
    if c.stale_pair && c.pairs.is_none() {
        return Err("missing control section".into());
    }
    let state_path = c.state_path.as_ref().ok_or("state path required")?;
    let outbox = c.outbox_path.as_ref().ok_or("outbox path required")?;
    let ack_path = c.acks_path.as_ref().ok_or("acks path required")?;
    let tolerance = c.wall_jump_tolerance_s.ok_or("clock tolerance required")?;
    // Validate all constructors before creating even the lock file.
    if c.stale_pair {
        let p = c.pairs.as_ref().unwrap();
        let mut names = BTreeSet::new();
        for g in &p.groups {
            if g.group.is_empty() || !names.insert(&g.group) {
                return Err("invalid group".into());
            }
            delivery::Delivery::new(format!("stale-pair:{}", g.group), 0, &p.config)?;
        }
    }
    #[cfg(not(unix))]
    return Err("unsupported locking target".into());
    #[cfg(unix)]
    let _lock = {
        use std::os::fd::AsRawFd;
        let mut name = state_path.as_os_str().to_os_string();
        name.push(".lock");
        let f = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(name)
            .map_err(|_| "lock open failed")?;
        if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock {
                return Ok(json!({"skipped":"locked"}));
            }
            return Err("lock failed".into());
        }
        f
    };
    let mut s: State = match fs::read(state_path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "invalid state")?,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => State {
            schema_version: 1,
            clock: None,
            pairs: BTreeMap::new(),
        },
        Err(_) => return Err("state read failed".into()),
    };
    if s.schema_version != 1 {
        return Err("state schema".into());
    }
    let (clock, jump) = advance(s.clock.as_ref(), raw, wall, tolerance);
    let now = clock.total_ms / 1000;
    let mut diagnostics = Vec::<String>::new();
    let mut acks = BTreeSet::new();
    match fs::read_to_string(ack_path) {
        Ok(text) => {
            for line in text.lines() {
                if line.starts_with("heartbeat:") && line[10..].parse::<u64>().is_err()
                    || !line.contains(':')
                {
                    diagnostics.push("invalid acknowledgement".into());
                } else {
                    acks.insert(line.to_owned());
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
        Err(_) => diagnostics.push("acks read failed".into()),
    }
    let heartbeat = acks
        .iter()
        .filter_map(|a| a.strip_prefix("heartbeat:")?.parse::<u64>().ok())
        .max();
    let mut opened = Vec::new();
    let mut attempts = 0;
    let mut send_errors = Vec::new();
    let mut send = |kind: &str, id: &str, group: Option<&str>| {
        attempts += 1;
        let mut line = json!({"kind":kind,"id":id});
        if let Some(g) = group {
            line["group"] = json!(g);
        }
        let result = append(outbox, line);
        if let Err(e) = &result {
            send_errors.push(e.clone());
        }
        result
    };
    if c.stale_pair {
        let p = c.pairs.as_ref().unwrap();
        for g in &p.groups {
            let namespace = format!("stale-pair:{}", g.group);
            if !s.pairs.contains_key(&g.group) {
                s.pairs.insert(
                    g.group.clone(),
                    PairState {
                        detector: pair::State::default(),
                        delivery: delivery::Delivery::new(namespace.clone(), now, &p.config)?,
                        active: None,
                    },
                );
            }
            let state = s.pairs.get_mut(&g.group).unwrap();
            let finding = state.detector.observe(
                &p.config,
                &fs::read(&g.snapshot_path).unwrap_or_default(),
                pair::Clock {
                    wall,
                    monotonic: now,
                    discontinuity: jump.then(|| "wall jump".into()),
                },
            );
            diagnostics.extend(finding.diagnostics.clone());
            if finding.new_alarm {
                let id = format!("{namespace}:pair:{}", finding.episode.unwrap());
                opened.push(id.clone());
                state.active = Some(id);
            }
            let ack = state.active.as_deref().filter(|id| acks.contains(*id));
            let mut sink = |id: &str| send("alarm", id, Some(&g.group));
            state.delivery.tick(Some(&finding), ack, now, &mut sink);
            state.delivery.silence.tick(heartbeat, now, &mut sink);
        }
    }
    if let Err(e) = send("heartbeat", &format!("heartbeat:{now}"), None) {
        diagnostics.push(e);
    }
    s.clock = Some(clock);
    diagnostics.extend(send_errors);
    let parent = state_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or("state directory required")?;
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|_| "state temporary file")?;
    serde_json::to_writer(&mut temp, &s).map_err(|_| "state encode")?;
    temp.as_file().sync_all().map_err(|_| "state sync")?;
    temp.persist(state_path).map_err(|_| "state rename")?;
    Ok(
        json!({"enabled":true,"stale_pair":c.stale_pair,"groups":if c.stale_pair { c.pairs.as_ref().unwrap().groups.iter().map(|g| &g.group).collect::<Vec<_>>() } else { vec![] },"alarms_opened":opened,"attempts_sent":attempts,"diagnostics":diagnostics}),
    )
}
#[cfg(test)]
#[path = "fleet_foundation_tick_tests.rs"]
mod tests;
