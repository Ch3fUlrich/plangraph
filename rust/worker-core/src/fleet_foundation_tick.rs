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
    #[serde(default)]
    hysteresis: bool,
    hysteresis_config: Option<crate::fleet_foundation_hysteresis::Config>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    hysteresis: Option<crate::fleet_foundation_hysteresis::State>,
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
    let pairs = if c.stale_pair {
        Some(c.pairs.as_ref().ok_or("missing control section")?)
    } else {
        None
    };
    let state_path = c.state_path.as_ref().ok_or("state path required")?;
    let parent = state_path.parent().ok_or("state directory required")?;
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    if !parent.is_dir() {
        return Err("state directory required".into());
    }
    let hysteresis = if c.hysteresis {
        let h = c
            .hysteresis_config
            .as_ref()
            .ok_or("missing control section")?;
        h.validate()?;
        Some(h)
    } else {
        None
    };
    let outbox = c.outbox_path.as_ref().ok_or("outbox path required")?;
    let ack_path = c.acks_path.as_ref().ok_or("acks path required")?;
    let tolerance = c.wall_jump_tolerance_s.ok_or("clock tolerance required")?;
    // Validate all constructors before creating even the lock file.
    if let Some(p) = pairs {
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
            hysteresis: None,
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
    // Reserve the save file before any externally visible delivery.
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|_| "state temporary file")?;
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
    if let Some(p) = pairs {
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
        }
    }
    if let Some(p) = pairs {
        for g in &p.groups {
            let namespace = format!("stale-pair:{}", g.group);
            let state = s.pairs.get_mut(&g.group).ok_or("missing group state")?;
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
            record_alarm(
                &finding,
                &namespace,
                &mut state.active,
                &mut opened,
                &mut diagnostics,
            );
            let ack = state.active.as_deref().filter(|id| acks.contains(*id));
            let mut sink = |id: &str| send("alarm", id, Some(&g.group));
            state.delivery.tick(Some(&finding), ack, now, &mut sink);
            state.delivery.silence.tick(heartbeat, now, &mut sink);
        }
    }
    let observations = if let Some(h) = hysteresis {
        crate::fleet_foundation_hysteresis::tick(
            h,
            &mut s.hysteresis,
            wall,
            now,
            jump,
            &acks,
            heartbeat,
            &mut |id| send("alarm", id, None),
            &mut opened,
            &mut diagnostics,
        )
        .unwrap_or_else(|error| {
            diagnostics.push(error);
            Vec::new()
        })
    } else {
        Vec::new()
    };
    let _ = send("heartbeat", &format!("heartbeat:{now}"), None);
    s.clock = Some(clock);
    diagnostics.extend(send_errors);
    serde_json::to_writer(&mut temp, &s).map_err(|_| "state encode")?;
    temp.as_file().sync_all().map_err(|_| "state sync")?;
    temp.persist(state_path).map_err(|_| "state rename")?;
    Ok(
        json!({"observations":observations,"enabled":true,"stale_pair":c.stale_pair,"groups":pairs.map_or_else(Vec::new, |p| p.groups.iter().map(|g| &g.group).collect::<Vec<_>>()),"alarms_opened":opened,"attempts_sent":attempts,"diagnostics":diagnostics}),
    )
}
fn record_alarm(
    finding: &pair::Finding,
    namespace: &str,
    active: &mut Option<String>,
    opened: &mut Vec<String>,
    diagnostics: &mut Vec<String>,
) {
    if finding.new_alarm {
        if let Some(episode) = finding.episode {
            let id = format!("{namespace}:pair:{episode}");
            opened.push(id.clone());
            *active = Some(id);
        } else {
            diagnostics.push("new alarm missing episode".into());
        }
    }
}
#[cfg(test)]
#[path = "fleet_foundation_tick_state_tests.rs"]
mod state_tests;
#[cfg(test)]
#[path = "fleet_foundation_tick_tests.rs"]
mod tests;
