//! Thin I/O adapter. No selection logic belongs here; these calls can become activities.
use super::adapters::{Adapters, Env, Keeper, Transport};
use super::*;
use super::{capacity, sources, windows_ram};
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

/// The live capacity numbers of one decision line: the RAM views, load and pressure admission
/// reconciled and the slots they leave. Applies the live budget to `facts` and `policy`.
fn capacity_line(
    facts: &mut HostFacts,
    policy: &mut Policy,
    load: Option<(usize, f64)>,
    ram_views: Option<(u64, Option<windows_ram::WindowsRam>)>,
) -> Result<Option<Value>, String> {
    let Some((cpus, load)) = load else {
        return Ok(None);
    };
    let free_ram_bytes = facts.free_ram_bytes;
    let budget = capacity::apply(facts, policy, cpus, load)?;
    Ok(Some(json!({
        "host":facts.host, "cpus":cpus, "load1":load, "running_writers":facts.running_writers,
        "free_ram_bytes":free_ram_bytes, "ram_floor_bytes":policy.ram_floor_bytes,
        "wsl_free_ram_bytes":ram_views.map(|v| v.0),
        "windows_free_ram_bytes":ram_views.and_then(|v| v.1).map(|w| w.free_bytes),
        "windows_ram_source":ram_views.and_then(|v| v.1).map(|w| w.source),
        "windows_ram_floor_bytes":ram_views.and_then(|v| v.1).map(|_| policy.windows_ram_floor_bytes),
        "ram_per_writer_bytes":policy.ram_per_run_bytes,
        "slots_by_ram":budget.slots_by_ram, "new_writers_allowed":budget.new_writers_allowed,
        "load_paced":budget.load_paced, "writer_limit":budget.limit,
        "cpu_pressure_avg300":facts.cpu_pressure_avg300,
        "cpu_pressure_max_percent":policy.cpu_pressure_max_percent,
        "cpu_pressure_source":budget.pressure_source,
        "free_disk_bytes":facts.free_disk_bytes,
        "quota_remaining_percent":facts.quota_remaining_percent, "observed_at":facts.observed_at,
    })))
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
    let running = sources::runs(env, &o.repo, &ready, state, home, o.dry)?;
    let eligible = pick(&ready, &running, &policy).is_some();
    if !o.live && !eligible {
        return Ok(json!({"decision":"idle","reason":"no_eligible_item"}));
    }
    let host = env
        .get("PLANGRAPH_HOST_NAME")
        .map(String::as_str)
        .or(local.as_deref());
    let (sample, load, ram_views) = if o.live {
        let host = host
            .filter(|host| !host.trim().is_empty())
            .ok_or("live capacity requires a configured host name")?;
        let disk_root = state.ancestors().find(|path| path.is_dir()).unwrap_or(home);
        let m = capacity::sample(
            host,
            disk_root,
            env,
            &policy,
            state,
            o.dry,
            windows_ram::read_powershell,
        )?;
        (
            m.facts,
            Some((m.cpus, m.load)),
            Some((m.wsl_free_ram_bytes, m.windows)),
        )
    } else {
        let sample = serde_json::from_value(sources::read_json(&o.facts)?)
            .map_err(|_| "invalid host facts")?;
        (sample, None, None)
    };
    let mut facts = match sources::facts(
        env,
        &o.repo,
        sample,
        &running,
        home,
        &policy,
        local.as_deref(),
    )? {
        sources::FactsOutcome::Ready(facts) => facts,
        // A stale center snapshot is a routine refusal that names its age, not a fault; the
        // capacity line carries the same numbers (writers, RAM reservation) as any other tick.
        sources::FactsOutcome::QuotaStale { reason, mut facts } => {
            let capacity = capacity_line(&mut facts, &mut policy, load, ram_views)?;
            return Ok(json!({"decision":"refused","reason":reason,"capacity":capacity}));
        }
    };
    let measurement = capacity_line(&mut facts, &mut policy, load, ram_views)?;
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
            "host":"dell", "free_ram_bytes":free_ram,
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
    fn without_a_psi_file_load_falls_back_to_one_new_writer_per_tick_and_says_so() {
        // The workstation of 2026-11-24: 32 cpus, load1 ~100, 86 GB free, no writer.
        let mut policy = Policy::default();
        let mut facts = live_facts(0, 86_000_000_000);
        let b = super::super::capacity::apply(&mut facts, &mut policy, 32, 100.0).unwrap();
        assert_eq!(b.slots_by_ram, 20);
        assert_eq!((b.new_writers_allowed, b.load_paced, b.limit), (1, true, 1));
        assert_eq!(b.pressure_source, "load_fallback");
        assert_eq!(admit(&facts, &policy), Admission::Admit);
        facts.running_writers = 1;
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
    }
    #[test]
    fn cpu_pressure_refuses_above_the_limit_and_admits_at_or_below_with_ram_above_the_floor() {
        let mut policy = Policy::default();
        let case = |pressure: f64, policy: &mut Policy| {
            let mut facts = live_facts(0, 86_000_000_000);
            facts.cpu_pressure_avg300 = Some(pressure);
            let b = super::super::capacity::apply(&mut facts, policy, 32, 151.0).unwrap();
            (admit(&facts, policy), b)
        };
        let (decision, b) = case(51.0, &mut policy);
        assert_eq!(
            decision,
            Admission::Refuse("cpu_pressure avg300=51 > 50".into())
        );
        // With PSI present load no longer paces: the slots stay the free-RAM slots.
        assert_eq!(
            (b.pressure_source, b.load_paced, b.limit),
            ("psi", false, 20)
        );
        assert_eq!(case(49.0, &mut policy).0, Admission::Admit);
        assert_eq!(case(50.0, &mut policy).0, Admission::Admit);
        assert_eq!(
            case(70.15, &mut policy).0,
            Admission::Refuse("cpu_pressure avg300=70.15 > 50".into())
        );
        policy.cpu_pressure_max_percent = 80.0;
        assert_eq!(case(70.15, &mut policy).0, Admission::Admit);
    }
    #[test]
    fn the_capacity_line_carries_sampled_ram_load_and_writers() {
        let mut policy = Policy::default();
        let mut facts = live_facts(2, 20_000_000_000);
        let views = Some((85_000_000_000, None));
        let line = capacity_line(&mut facts, &mut policy, Some((32, 5.0)), views)
            .unwrap()
            .unwrap();
        assert_eq!(line["running_writers"], 2);
        assert_eq!(line["free_ram_bytes"], 20_000_000_000u64);
        assert_eq!(line["wsl_free_ram_bytes"], 85_000_000_000u64);
        assert_eq!(line["load1"], 5.0);
        assert_eq!(line["slots_by_ram"], 4);
        assert!(capacity_line(&mut facts, &mut policy, None, None)
            .unwrap()
            .is_none());
    }
    #[test]
    fn psi_text_gives_some_avg300_and_other_text_gives_none() {
        let text = "some avg10=63.12 avg60=65.26 avg300=70.15 total=21880727490\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n";
        assert_eq!(
            super::super::capacity::parse_cpu_pressure(text),
            Some(70.15)
        );
        for bad in ["", "full avg300=3.0", "some avg10=1.0", "some avg300=x"] {
            assert_eq!(super::super::capacity::parse_cpu_pressure(bad), None);
        }
    }
    #[test]
    fn a_host_the_policy_does_not_list_refuses_by_name_even_with_free_ram() {
        let mut policy = Policy::default();
        let mut facts = live_facts(0, 86_000_000_000);
        facts.host = "mystery".into();
        super::super::capacity::apply(&mut facts, &mut policy, 32, 1.0).unwrap();
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("unknown_host:mystery".into())
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
            "quota_stale: center quota snapshot is 5400s old (limit 900s)",
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
