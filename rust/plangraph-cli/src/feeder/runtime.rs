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
    /// Where `--live-facts` reads the kernel counters; tests point it at a fixture.
    proc: PathBuf,
}
fn options(home: &Path, args: &[String]) -> Result<Options, String> {
    let mut o = Options {
        dry: false,
        live: false,
        policy: "deploy/feeder/policy.toml".into(),
        facts: home.join(".config/plangraph/feeder-facts.json"),
        rules: "deploy/feeder/house-rules.md".into(),
        repo: std::env::current_dir().map_err(|_| "cwd unavailable")?,
        proc: "/proc".into(),
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
    decide(
        home,
        env,
        o,
        state,
        policy,
        local.as_deref(),
        &ready,
        &running,
    )
}

/// The capacity line of a live decision: why this host admits or refuses. `load1` is null and
/// `load_paced` false when the host reports no load; `writer_limit` is null for a host the
/// policy does not list.
fn capacity_line(
    facts: &HostFacts,
    free_ram_bytes: u64,
    policy: &Policy,
    cpu: capacity::Cpu,
    budget: &capacity::Budget,
) -> Value {
    json!({
        "host":facts.host, "cpus":cpu.cpus, "load1":cpu.load1, "running_writers":facts.running_writers,
        "free_ram_bytes":free_ram_bytes, "ram_floor_bytes":policy.ram_floor_bytes,
        "ram_per_writer_bytes":policy.ram_per_run_bytes,
        "slots_by_ram":budget.slots_by_ram, "new_writers_allowed":budget.new_writers_allowed,
        "load_paced":budget.load_paced, "writer_limit":budget.limit,
        "free_disk_bytes":facts.free_disk_bytes,
        "quota_remaining_percent":facts.quota_remaining_percent, "observed_at":facts.observed_at,
    })
}

/// Everything after the keeper and run reads. `--live-facts` derives the writer limit from the
/// RAM budget of a live sample; `--facts` keeps the limit the policy lists. Both refuse a host
/// the policy does not list by name, in `admit`.
#[allow(clippy::too_many_arguments)]
fn decide(
    home: &Path,
    env: &Env,
    o: &Options,
    state: &Path,
    mut policy: Policy,
    local: Option<&str>,
    ready: &Value,
    running: &[Run],
) -> Result<Value, String> {
    let eligible = pick(ready, running, &policy).is_some();
    if !o.live && !eligible {
        return Ok(json!({"decision":"idle","reason":"no_eligible_item"}));
    }
    let host = env.get("PLANGRAPH_HOST_NAME").map(String::as_str).or(local);
    let (sample, cpu) = if o.live {
        let host = host
            .filter(|host| !host.trim().is_empty())
            .ok_or("live capacity requires a configured host name")?;
        let disk_root = state.ancestors().find(|path| path.is_dir()).unwrap_or(home);
        let (sample, cpu) = capacity::sample(host, disk_root, env, &o.proc)?;
        (sample, Some(cpu))
    } else {
        let sample = serde_json::from_value(sources::read_json(&o.facts)?)
            .map_err(|_| "invalid host facts")?;
        (sample, None)
    };
    let mut facts = sources::facts(env, &o.repo, sample, running, home, &policy, local)?;
    let measurement = if let Some(cpu) = cpu {
        let free_ram_bytes = facts.free_ram_bytes;
        let budget = capacity::apply(&mut facts, &mut policy, cpu)?;
        Some(capacity_line(&facts, free_ram_bytes, &policy, cpu, &budget))
    } else {
        None
    };
    if !eligible {
        return Ok(json!({"decision":"idle","reason":"no_eligible_item","capacity":measurement}));
    }
    let rules = fs::read_to_string(&o.rules).map_err(|_| "house rules unreadable")?;
    let reviews = home.join(".local/share/plangraph/handoffs/plan-order/reviews");
    let mut row = tick(
        ready,
        running,
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
    fn live_facts(host: &str, running: usize, free_ram: u64) -> HostFacts {
        serde_json::from_value(json!({
            "host":host, "free_ram_bytes":free_ram,
            "free_disk_bytes":20000000000u64, "quota_remaining_percent":80,
            "observed_at":1000, "now":1000, "running_writers":running
        }))
        .unwrap()
    }
    fn cpu(cpus: usize, load1: Option<f64>, psi: bool) -> capacity::Cpu {
        capacity::Cpu { cpus, load1, psi }
    }
    #[test]
    fn live_slots_follow_free_ram_above_the_floor_not_load() {
        let mut policy = Policy::default();
        // 7 GB free - 3 GB floor = 4 GB = one 4 GB writer on top of the 5 running.
        let mut facts = live_facts("dell", 5, 7_000_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(12, Some(0.5), false)).unwrap();
        assert_eq!((b.slots_by_ram, b.limit, b.load_paced), (1, Some(6), false));
        assert_eq!(facts.free_ram_bytes, 4_000_000_000);
        assert_eq!(admit(&facts, &policy), Admission::Admit);
        facts.running_writers = 6;
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
        // Load 11.5 on 12 cpus changes nothing: only RAM sets the slots.
        let mut facts = live_facts("dell", 5, 7_000_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(12, Some(11.5), false)).unwrap();
        assert_eq!(b.limit, Some(6));
        assert_eq!(admit(&facts, &policy), Admission::Admit);
    }
    #[test]
    fn without_a_psi_file_load_falls_back_to_one_new_writer_per_tick_and_says_so() {
        // The workstation of 2026-11-24: 32 cpus, load1 ~100, 86 GB free, no writer.
        let mut policy = Policy::default();
        policy.max_writers.insert("workstation".into(), 8);
        let mut facts = live_facts("workstation", 0, 86_000_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(32, Some(100.0), false)).unwrap();
        assert_eq!(b.slots_by_ram, 20);
        assert_eq!(
            (b.new_writers_allowed, b.load_paced, b.limit),
            (1, true, Some(1))
        );
        assert_eq!(admit(&facts, &policy), Admission::Admit);
        facts.running_writers = 1;
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
        // With a PSI file the same load does not pace.
        let mut facts = live_facts("workstation", 0, 86_000_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(32, Some(100.0), true)).unwrap();
        assert_eq!((b.new_writers_allowed, b.load_paced), (20, false));
    }
    #[test]
    fn a_host_the_policy_does_not_list_refuses_by_name_even_with_free_ram() {
        let mut policy = Policy::default();
        let mut facts = live_facts("mystery", 0, 86_000_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(32, Some(1.0), false)).unwrap();
        assert_eq!(
            (b.slots_by_ram, b.new_writers_allowed, b.limit),
            (20, 0, None)
        );
        assert!(!policy.max_writers.contains_key("mystery"));
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("unknown_host:mystery".into())
        );
    }
    #[test]
    fn no_free_ram_above_the_floor_gives_no_slot_whatever_the_load() {
        let mut policy = Policy::default();
        let mut facts = live_facts("dell", 0, 3_500_000_000);
        let b = capacity::apply(&mut facts, &mut policy, cpu(32, Some(0.0), false)).unwrap();
        assert_eq!((b.slots_by_ram, b.limit), (0, Some(0)));
        assert_eq!(
            admit(&facts, &policy),
            Admission::Refuse("max_writers".into())
        );
    }

    /// A live decision on a fixture host: 16 000 000 kB available (3 slots above the 3 GB floor
    /// at 4 GB each), one eligible item, no quota snapshot (the last gate, `provider_quota`).
    struct Live {
        dir: tempfile::TempDir,
        env: Env,
        options: Options,
        ready: Value,
    }
    impl Live {
        fn new(host: &str, policy: &str) -> Self {
            let dir = tempfile::tempdir().unwrap();
            let at = |name: &str| dir.path().join(name);
            fs::create_dir_all(at("proc")).unwrap();
            fs::write(
                at("proc/meminfo"),
                "MemTotal: 32000000 kB\nMemAvailable: 16000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
            )
            .unwrap();
            fs::write(at("policy.toml"), format!("disk_floor_bytes = 1\n{policy}")).unwrap();
            fs::write(at("rules.md"), "rules").unwrap();
            // An empty PATH keeps the quota adapter from running python: no quota snapshot.
            let empty_path = at("empty-path");
            fs::create_dir_all(&empty_path).unwrap();
            let env = Env::from([
                ("PLANGRAPH_HOST_NAME".into(), host.into()),
                ("PATH".into(), empty_path.to_string_lossy().into()),
            ]);
            let options = Options {
                dry: true,
                live: true,
                policy: at("policy.toml"),
                facts: at("facts.json"),
                rules: at("rules.md"),
                repo: dir.path().to_path_buf(),
                proc: at("proc"),
            };
            let ready = json!({"items":[{"key":"plangraph:X","title":"x","priority":5,
                "created_at":"2026-01-01T00:00:00Z","state":"open","files":[]}]});
            Self {
                dir,
                env,
                options,
                ready,
            }
        }
        fn proc(&self, name: &str) -> PathBuf {
            self.dir.path().join("proc").join(name)
        }
        fn decide(&self) -> Value {
            let policy: Policy =
                toml::from_str(&fs::read_to_string(&self.options.policy).unwrap()).unwrap();
            decide(
                self.dir.path(),
                &self.env,
                &self.options,
                &self.dir.path().join("state"),
                policy,
                None,
                &self.ready,
                &[],
            )
            .unwrap()
        }
    }
    const SATURATED: &str = "1000000.0 1000000.0 1000000.0 1/2 3\n";

    #[test]
    #[cfg(unix)]
    fn live_decision_reports_ram_budget_and_psi_pacing() {
        use std::os::unix::fs::PermissionsExt;
        let live = Live::new("dell", "[max_writers]\ndell = 5\n");
        fs::write(live.proc("loadavg"), SATURATED).unwrap();
        // (pressure file state, expected pacing): only a missing file paces by load.
        for (state, paced) in [
            ("missing", true),
            ("valid", false),
            ("malformed", false),
            ("unreadable", false),
        ] {
            let dir = live.proc("pressure");
            let file = dir.join("cpu");
            let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o755));
            let _ = fs::remove_dir_all(&dir);
            match state {
                "missing" => {}
                _ => {
                    fs::create_dir_all(&dir).unwrap();
                    let text = if state == "malformed" {
                        "garbage"
                    } else {
                        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                    };
                    fs::write(&file, text).unwrap();
                    if state == "unreadable" {
                        fs::set_permissions(&dir, fs::Permissions::from_mode(0o000)).unwrap();
                    }
                }
            }
            let row = live.decide();
            let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o755));
            let c = &row["capacity"];
            assert_eq!(c["load_paced"], paced, "{state}");
            assert_eq!(c["slots_by_ram"], 3, "{state}");
            assert_eq!(
                c["new_writers_allowed"],
                if paced { 1 } else { 3 },
                "{state}"
            );
            assert_eq!(c["writer_limit"], if paced { 1 } else { 3 }, "{state}");
            assert_eq!(c["free_ram_bytes"], 16_384_000_000u64, "{state}");
            assert_eq!(c["ram_floor_bytes"], 3_000_000_000u64);
            assert_eq!(c["ram_per_writer_bytes"], 4_000_000_000u64);
            assert_eq!(c["load1"], 1_000_000.0);
            // Past max_writers and ram: the first gate that still refuses is the quota.
            assert_eq!(row["reason"], "provider_quota", "{state}");
        }
    }
    #[test]
    fn live_decision_uses_ram_when_load_is_unavailable() {
        // The configured cap of 0 would refuse the launch; the live RAM budget admits it.
        let live = Live::new("dell", "[max_writers]\ndell = 0\n");
        for loadavg in [None, Some("garbage")] {
            if let Some(text) = loadavg {
                fs::write(live.proc("loadavg"), text).unwrap();
            }
            let row = live.decide();
            let c = &row["capacity"];
            assert_eq!(c["load1"], Value::Null);
            assert_eq!(c["load_paced"], false);
            assert_eq!(
                (&c["slots_by_ram"], &c["writer_limit"]),
                (&json!(3), &json!(3))
            );
            assert_eq!(row["reason"], "provider_quota");
        }
    }
    #[test]
    fn live_decision_refuses_an_unlisted_host_by_name_with_its_ram_slots() {
        let live = Live::new("mystery", "[max_writers]\ndell = 5\n");
        fs::write(live.proc("loadavg"), "0.1 0.1 0.1 1/2 3\n").unwrap();
        let row = live.decide();
        let c = &row["capacity"];
        assert_eq!(row["reason"], "unknown_host:mystery");
        assert_eq!(c["slots_by_ram"], 3);
        assert_eq!(c["new_writers_allowed"], 0);
        assert_eq!(c["writer_limit"], Value::Null);
    }
    #[test]
    fn facts_file_keeps_the_configured_cap_and_refuses_an_unlisted_host_by_name() {
        let mut live = Live::new("mystery", "[max_writers]\ndell = 5\n");
        live.options.live = false;
        fs::write(
            &live.options.facts,
            json!({"host":"mystery","free_ram_bytes":86_000_000_000u64,
                "free_disk_bytes":20_000_000_000u64,"observed_at":chrono::Utc::now().timestamp()})
            .to_string(),
        )
        .unwrap();
        let row = live.decide();
        assert_eq!(row["reason"], "unknown_host:mystery");
        assert!(row.get("capacity").is_none());
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
