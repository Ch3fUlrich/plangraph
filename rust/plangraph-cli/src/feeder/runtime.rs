//! Thin I/O adapter. No selection logic belongs here; these calls can become activities.
use super::adapters::{Adapters, Env, Keeper, Transport};
use super::*;
use super::{capacity, sources};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug)]
struct Options {
    dry: bool,
    live: bool,
    policy: PathBuf,
    facts: PathBuf,
    rules: PathBuf,
    repo: PathBuf,
}
fn options(home: &Path, args: &[String]) -> Result<Options, String> {
    let mut o = Options {
        dry: false,
        live: false,
        policy: "deploy/feeder/policy.toml".into(),
        facts: home.join(".config/plangraph/feeder-facts.json"),
        rules: "deploy/feeder/house-rules.md".into(),
        repo: std::env::current_dir().map_err(|_| "cwd unavailable")?,
    };
    let mut once = false;
    let mut explicit_facts = false;
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--once" => once = true,
            "--dry-run" => o.dry = true,
            "--live-facts" => o.live = true,
            "--policy" | "--facts" | "--house-rules" | "--repo" => {
                let value = PathBuf::from(args.next().ok_or("option requires a path")?);
                match arg.as_str() { "--policy" => o.policy=value, "--facts" => { o.facts=value; explicit_facts = true; },
                    "--house-rules" => o.rules=value, _ => o.repo=value }
            }
            _ => return Err("usage: plangraph feed --once [--dry-run] [--live-facts | --facts PATH] [--policy PATH] [--house-rules PATH] [--repo PATH]".into()),
        }
    }
    if !once {
        return Err("feed requires --once; use the systemd timer for repetition".into());
    }
    if o.live && explicit_facts {
        return Err("--live-facts and --facts are mutually exclusive".into());
    }
    if !o.repo.is_absolute() {
        return Err("repo must be an absolute checkout path".into());
    }
    Ok(o)
}

fn decision(home: &Path, env: &Env, o: &Options, state: &Path) -> Result<Value, String> {
    let mut policy: Policy =
        toml::from_str(&fs::read_to_string(&o.policy).map_err(|_| "policy unreadable")?)
            .map_err(|_| "invalid policy")?;
    if !o.repo.is_absolute() {
        return Err("repo must be an absolute checkout path".into());
    }
    let local = sources::registry_policy(home, &mut policy)?;
    let ready = Keeper { env }.call("ready?fmt=json&all=1&agent=1&project=plangraph", None)?;
    validate_ready(&ready)?;
    let running = sources::runs(env, &o.repo, &ready, state, o.dry)?;
    let eligible = pick(&ready, &running, &policy).is_some();
    if !o.live && !eligible {
        return Ok(json!({"decision":"idle","reason":"no_eligible_item"}));
    }
    let host = env
        .get("PLANGRAPH_HOST_NAME")
        .map(String::as_str)
        .or(local.as_deref());
    let (sample, load) = if o.live {
        let host = host
            .filter(|host| !host.trim().is_empty())
            .ok_or("live capacity requires a configured host name")?;
        let disk_root = state.ancestors().find(|path| path.is_dir()).unwrap_or(home);
        let (sample, cpus, load) = capacity::sample(host, disk_root, env)?;
        (sample, Some((cpus, load)))
    } else {
        let sample = serde_json::from_value(sources::read_json(&o.facts)?)
            .map_err(|_| "invalid host facts")?;
        (sample, None)
    };
    let mut facts = sources::facts(
        env,
        &o.repo,
        sample,
        &running,
        home,
        &policy,
        local.as_deref(),
    )?;
    let measurement = if let Some((cpus, load)) = load {
        let free_ram_bytes = facts.free_ram_bytes;
        let budget = capacity::apply(&mut facts, &mut policy, cpus, load)?;
        Some(json!({
            "host":facts.host, "cpus":cpus, "load1":load, "running_writers":facts.running_writers,
            "free_ram_bytes":free_ram_bytes, "ram_floor_bytes":policy.ram_floor_bytes,
            "ram_per_writer_bytes":policy.ram_per_run_bytes,
            "slots_by_ram":budget.slots_by_ram, "new_writers_allowed":budget.new_writers_allowed,
            "load_paced":budget.load_paced, "writer_limit":budget.limit,
            "free_disk_bytes":facts.free_disk_bytes,
            "quota_remaining_percent":facts.quota_remaining_percent, "observed_at":facts.observed_at,
        }))
    } else {
        None
    };
    if !eligible {
        return Ok(json!({"decision":"idle","reason":"no_eligible_item","capacity":measurement}));
    }
    let rules = fs::read_to_string(&o.rules).map_err(|_| "house rules unreadable")?;
    let reviews = home.join(".local/share/plangraph/handoffs/plan-order/reviews");
    let mut row = tick(
        &ready,
        &running,
        &facts,
        &policy,
        &rules,
        &o.repo.to_string_lossy(),
        &reviews.to_string_lossy(),
        o.dry,
        &mut Adapters {
            keeper: Keeper { env },
            env,
            repo: o.repo.clone(),
            state: state.into(),
            host: facts.host.clone(),
        },
    );
    if let Some(measurement) = measurement {
        row["capacity"] = measurement;
    }
    Ok(row)
}

#[cfg(unix)]
fn lock(state: &Path) -> Result<File, String> {
    use std::os::fd::AsRawFd;
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(state.join("feed.lock"))
        .map_err(|_| "cannot open feeder lock")?;
    // SAFETY: valid owned fd; flock is released when the File drops.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("feeder_already_running".into());
    }
    Ok(file)
}
#[cfg(not(unix))]
fn lock(_: &Path) -> Result<File, String> {
    Err("feeder requires Unix locking".into())
}

fn append_event(state: &Path, row: &Value) -> std::io::Result<()> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(state.join("events.jsonl"))?
        .write_all(format!("{row}\n").as_bytes())
}

fn exit_code(row: &Value) -> i32 {
    if row["error"] == true {
        2
    } else {
        0
    }
}

pub fn run(home: &Path, env: &Env, args: &[String]) -> i32 {
    let o = match options(home, args) {
        Ok(o) => o,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let state = home.join(".local/state/plangraph/feeder");
    let mut held_lock = None;
    let result = (|| {
        if !o.dry {
            fs::create_dir_all(&state).map_err(|_| "cannot create feeder state")?;
            held_lock = Some(lock(&state)?);
        }
        decision(home, env, &o, &state)
    })();
    let (mut row, code) = match result {
        Ok(row) => {
            let code = exit_code(&row);
            (row, code)
        }
        Err(e) => (json!({"decision":"refused","reason":e,"error":true}), 2),
    };
    row["at"] = json!(chrono::Utc::now().to_rfc3339());
    row["dry_run"] = json!(o.dry);
    if !o.dry {
        let log = append_event(&state, &row);
        if log.is_err() {
            eprintln!("cannot append feeder event");
            println!("{row}");
            return 2;
        }
    }
    println!("{row}");
    code
}

#[cfg(test)]
mod tests {
    use super::*;
    fn live_facts(running: usize, free_ram: u64) -> HostFacts {
        serde_json::from_value(json!({
            "host":"worker", "free_ram_bytes":free_ram,
            "free_disk_bytes":20000000000u64, "quota_remaining_percent":80,
            "observed_at":1000, "now":1000, "running_writers":running
        }))
        .unwrap()
    }
    #[test]
    fn live_slots_follow_free_ram_above_the_floor_not_load() {
        let mut policy = Policy::default();
        // 7 GB free - 3 GB floor = 4 GB = one 4 GB writer on top of the 5 running.
        let mut facts = live_facts(5, 7_000_000_000);
        let b = super::super::capacity::apply(&mut facts, &mut policy, 12, 0.5).unwrap();
        assert_eq!((b.slots_by_ram, b.limit, b.load_paced), (1, 6, false));
        assert_eq!(facts.free_ram_bytes, 4_000_000_000);
        assert_eq!(admit(&facts, &policy), Admission::Admit);
        facts.running_writers = 6;
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
        // Load 11.5 on 12 cpus changes nothing: only RAM sets the slots.
        let mut facts = live_facts(5, 7_000_000_000);
        let b = super::super::capacity::apply(&mut facts, &mut policy, 12, 11.5).unwrap();
        assert_eq!(b.limit, 6);
        assert_eq!(admit(&facts, &policy), Admission::Admit);
    }
    #[test]
    fn a_loaded_host_with_free_ram_admits_by_ram_and_paces_to_one_new_writer() {
        // The workstation of 2026-11-24: 32 cpus, load1 ~100, 86 GB free, no writer.
        let mut policy = Policy::default();
        let mut facts = live_facts(0, 86_000_000_000);
        let b = super::super::capacity::apply(&mut facts, &mut policy, 32, 100.0).unwrap();
        assert_eq!(b.slots_by_ram, 20);
        assert_eq!((b.new_writers_allowed, b.load_paced, b.limit), (1, true, 1));
        assert_eq!(admit(&facts, &policy), Admission::Admit);
        facts.running_writers = 1;
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
    }
    #[test]
    fn no_free_ram_above_the_floor_gives_no_slot_whatever_the_load() {
        let mut policy = Policy::default();
        let mut facts = live_facts(0, 3_500_000_000);
        let b = super::super::capacity::apply(&mut facts, &mut policy, 32, 0.0).unwrap();
        assert_eq!((b.slots_by_ram, b.limit), (0, 0));
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
    }
    #[test]
    fn routine_admission_refusals_are_healthy_unit_exits() {
        for reason in [
            "max_writers",
            "ram",
            "disk",
            "provider_quota",
            "kill_switch",
        ] {
            assert_eq!(exit_code(&json!({"decision":"refused","reason":reason})), 0);
        }
        assert_eq!(
            exit_code(&json!({"decision":"refused","reason":"keeper unavailable","error":true})),
            2
        );
    }
    #[test]
    #[cfg(unix)]
    fn lock_excludes_concurrent_ticks_then_reopens() {
        let d = tempfile::tempdir().unwrap();
        let first = lock(d.path()).unwrap();
        assert!(lock(d.path()).is_err());
        drop(first);
        assert!(lock(d.path()).is_ok());
    }
    #[test]
    fn concurrent_events_remain_independent_json_lines() {
        let d = tempfile::tempdir().unwrap();
        std::thread::scope(|scope| {
            for writer in 0..8 {
                let path = d.path();
                scope.spawn(move|| {
                for n in 0..16 {append_event(path,&json!({"decision":"refused","writer":writer,"n":n,"reason":"x".repeat(2048)})).unwrap();}
            });
            }
        });
        let text = fs::read_to_string(d.path().join("events.jsonl")).unwrap();
        let rows: Vec<Value> = text
            .lines()
            .map(|s| serde_json::from_str(s).unwrap())
            .collect();
        assert_eq!(rows.len(), 128);
        let unique: std::collections::BTreeSet<_> = rows
            .iter()
            .map(|v| (v["writer"].as_u64().unwrap(), v["n"].as_u64().unwrap()))
            .collect();
        assert_eq!(unique.len(), 128);
    }
    #[test]
    fn relative_repo_is_refused_before_any_read_or_claim() {
        let d = tempfile::tempdir().unwrap();
        let error = options(
            d.path(),
            &[
                "--once".into(),
                "--dry-run".into(),
                "--repo".into(),
                "relative".into(),
            ],
        )
        .unwrap_err();
        assert!(error.contains("absolute"));
    }
}
