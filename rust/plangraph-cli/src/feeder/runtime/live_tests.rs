//! Live-facts decisions: the RAM budget, PSI pacing and unknown-host refusal.
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

/// Fixtures that drive `decide` with a Linux `/proc` directory and a stub quota adapter.
#[cfg(target_os = "linux")]
mod proc_fixture {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A live decision on a fixture host: 16 000 000 kB available (3 slots above the 3 GB floor
    /// at 4 GB each), one eligible item and a fresh codex quota row. The quota adapter is a
    /// `python3` stub on the fixture's own PATH, so the decision can end in `would_spawn`.
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
            let bin = at("bin");
            fs::create_dir_all(&bin).unwrap();
            let row = json!([{"provider":"codex","window_key":"w","remaining_percentage":80,
                "created_at":chrono::Utc::now().timestamp()}]);
            let stub = bin.join("python3");
            fs::write(&stub, format!("#!/bin/sh\nprintf '%s' '{row}'\n")).unwrap();
            fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
            let env = Env::from([
                ("PLANGRAPH_HOST_NAME".into(), host.into()),
                ("PATH".into(), bin.to_string_lossy().into()),
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
    fn live_decision_reports_ram_budget_and_psi_pacing() {
        let live = Live::new("dell", "[max_writers]\ndell = 5\n");
        fs::write(live.proc("loadavg"), SATURATED).unwrap();
        // (pressure/cpu state, expected pacing): only a missing file paces by load. "not_a_dir"
        // makes the open fail with an error that is not NotFound, as an unreadable file does.
        for (state, paced) in [
            ("missing", true),
            ("valid", false),
            ("malformed", false),
            ("not_a_dir", false),
        ] {
            let dir = live.proc("pressure");
            let _ = fs::remove_dir_all(&dir);
            let _ = fs::remove_file(&dir);
            match state {
                "missing" => {}
                "not_a_dir" => fs::write(&dir, "").unwrap(),
                _ => {
                    fs::create_dir_all(&dir).unwrap();
                    let text = if state == "malformed" {
                        "garbage"
                    } else {
                        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                    };
                    fs::write(dir.join("cpu"), text).unwrap();
                }
            }
            let row = live.decide();
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
            assert_eq!(row["decision"], "would_spawn", "{state}: {row}");
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
            assert_eq!(row["decision"], "would_spawn", "{row}");
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
}
