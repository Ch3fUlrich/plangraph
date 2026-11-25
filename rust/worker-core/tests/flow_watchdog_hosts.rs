use serde_json::{json, Value};
use std::{fs, path::PathBuf, process::Command};

/// One watchdog state directory plus the environment its runs share. Nothing leaks in from the
/// machine: no host name, registry, watermark file or delivery hook unless a test sets one.
struct Fx {
    dir: tempfile::TempDir,
    env: Vec<(&'static str, String)>,
}

impl Fx {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join("config");
        fs::create_dir_all(&config).unwrap();
        let env = vec![("XDG_CONFIG_HOME", config.display().to_string())];
        Self { dir, env }
    }

    fn set(mut self, key: &'static str, value: impl Into<String>) -> Self {
        self.env.retain(|(k, _)| *k != key);
        self.env.push((key, value.into()));
        self
    }

    fn state(&self) -> PathBuf {
        self.dir.path().join("state")
    }

    fn snapshot(hosts: Value) -> Value {
        json!({"pace":66,"budget":66,"ci_failed":0,
        "ci_total":10,"infra_failed":0,"review_age_hours":0,"review_rounds":0,
        "critical_idle_hours":0,"coding_vm_runs":0,"worker_idle":false,
        "host_drift":false,"closes_recent":4,"closes_previous":4,"hosts":hosts})
    }

    fn run_in(&self, state: &std::path::Path, snapshot: &Value) -> std::process::Output {
        let input = self.dir.path().join("input.json");
        fs::write(&input, snapshot.to_string()).unwrap();
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_flow-watchdog"));
        cmd.args([input.as_os_str(), state.as_os_str()]);
        for key in [
            "PLANGRAPH_WATCHDOG_DELIVERY",
            "PLANGRAPH_FLOW_PUSH",
            "PLANGRAPH_HOST_NAME",
            "PLANGRAPH_DISK_WATERMARKS",
            "PLANGRAPH_HOST_TMP_PATH",
            "PLANGRAPH_SPAWN_STATE",
        ] {
            cmd.env_remove(key);
        }
        cmd.envs(self.env.iter().map(|(k, v)| (k, v)));
        cmd.output().unwrap()
    }

    /// Run on this fixture's state with these host rows; the run must succeed.
    fn run(&self, hosts: Value) -> String {
        let output = self.run_in(&self.state(), &Self::snapshot(hosts));
        assert!(output.status.success(), "{output:?}");
        self.active()
    }

    fn active(&self) -> String {
        fs::read_to_string(self.state().join("active")).unwrap_or_default()
    }

    fn queue(&self) -> Vec<Value> {
        fs::read(self.state().join("queue.json"))
            .map(|bytes| serde_json::from_slice(&bytes).unwrap())
            .unwrap_or_default()
    }

    /// The `active` keys of the queued events, in order.
    fn queued(&self) -> Vec<String> {
        self.queue()
            .iter()
            .map(|event| event["active"].as_str().unwrap().to_owned())
            .collect()
    }
}

fn row(host: &str, disks: Value) -> Value {
    json!({"host":host,"disks":disks,"checkout_head":"a","origin_main_head":"a",
        "behind_commits":0,"age_hours":0})
}

fn legacy(host: &str, tmp: f64, root: f64, work: Value) -> Value {
    json!({"host":host,"tmp_used_percent":tmp,"root_used_percent":root,
        "work_used_percent":work,"checkout_head":"a","origin_main_head":"a",
        "behind_commits":0,"age_hours":0})
}

fn disk(device: &str, used: f64) -> Value {
    json!({"device":device,"used_percent":used})
}

fn measured(device: &str, used: f64, consumers: Value) -> Value {
    json!({"device":device,"used_percent":used,"consumers":consumers})
}

fn active(host: Value) -> String {
    Fx::new().run(json!([host]))
}

#[test]
fn disk_usage_is_strictly_above_85_on_either_filesystem() {
    for (tmp, root, fires) in [(85.0, 85.0, false), (85.1, 0.0, true), (0.0, 85.1, true)] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":tmp,
            "root_used_percent":root,"checkout_head":"a","origin_main_head":"a",
            "behind_commits":0,"age_hours":100}));
        assert_eq!(result.contains("host-disk"), fires, "{result}");
    }
}

#[test]
fn hot_work_disks_raise_one_host_alarm() {
    let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":86,
        "root_used_percent":0,"work_used_percent":[90,99],"checkout_head":"a",
        "origin_main_head":"a","behind_commits":0,"age_hours":100}));
    assert_eq!(result.matches("host-disk").count(), 1, "{result}");
    let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":0,
        "root_used_percent":0,"work_used_percent":[85,85.1],"checkout_head":"a",
        "origin_main_head":"a","behind_commits":0,"age_hours":100}));
    assert_eq!(result.matches("host-disk").count(), 1, "{result}");
}

#[test]
fn checkout_alerts_by_commit_count_or_age_only_when_behind() {
    for (head, commits, age, fires) in [
        ("a", 10, 24.0, false),
        ("a", 11, 0.0, true),
        ("a", 1, 24.1, true),
        ("a", 1, 48.0, true),
        ("b", 0, 48.0, false),
        ("a", 0, 48.0, false),
    ] {
        let result = active(serde_json::json!({"host":"fixture","tmp_used_percent":0,
            "root_used_percent":0,"checkout_head":head,"origin_main_head":"b",
            "behind_commits":commits,"age_hours":age}));
        assert_eq!(result.contains("host-code"), fires, "{result}");
        if fires {
            assert!(result.contains("host-code: host=\"fixture\""));
        }
    }
}

#[test]
fn cat_hook_test_crates_are_unix_only() {
    for source in [
        include_str!("flow_watchdog.rs"),
        include_str!("flow_watchdog_regressions.rs"),
    ] {
        assert!(source.starts_with("#![cfg(unix)]"));
    }
}

impl Fx {
    fn marks(&self, rows: Value) {
        fs::write(self.dir.path().join("marks.json"), rows.to_string()).unwrap();
    }

    /// This fixture is the local host: it is named `fixture` and reads its own watermark file.
    fn local(self) -> Self {
        let marks = self.dir.path().join("marks.json").display().to_string();
        self.set("PLANGRAPH_HOST_NAME", "fixture")
            .set("PLANGRAPH_DISK_WATERMARKS", marks)
            .at(1000)
    }

    fn at(self, now: u64) -> Self {
        self.set("PLANGRAPH_WATCHDOG_NOW", now.to_string())
    }
}

fn dirs(rows: &[(&str, u64)], measured_at: u64) -> Value {
    json!({"free":10,"total":100,"measured_at":measured_at,
        "directories":rows.iter().map(|(p, b)| json!({"path":p,"bytes":b})).collect::<Vec<_>>()})
}

fn sorted(mut keys: Vec<String>) -> Vec<String> {
    keys.sort();
    keys
}

fn bare(host: &str, device: &str) -> String {
    format!("host-disk: host=\"{host}\" fs=\"{device}\"")
}

fn measured_key(host: &str, device: &str) -> String {
    format!("{} measured", bare(host, device))
}

#[cfg(unix)]
fn root_device() -> String {
    use std::os::unix::fs::MetadataExt;
    fs::metadata("/").unwrap().dev().to_string()
}

#[cfg(unix)]
#[test]
fn overlapping_root_and_work_crossings_ignore_a_third_device() {
    let root = root_device();
    let fx = Fx::new().local();
    // The work mount is measured by its collector and the watermark file holds a third device.
    fx.marks(json!({"9001": dirs(&[("/work/w", 9)], 900), "9002": dirs(&[("/third/t", 8)], 900)}));
    let work = measured("9001", 92.0, json!([{"path":"/work/w","bytes":9}]));
    fx.run(json!([row(
        "fixture",
        json!([disk(&root, 90.0), work.clone()])
    )]));
    assert_eq!(
        sorted(fx.queued()),
        sorted(vec![
            bare("fixture", &root),
            measured_key("fixture", "9001")
        ])
    );
    // The root measurement arrives later: root queues its own measured alert once.
    fx.marks(json!({root.as_str(): dirs(&[("/root/r", 7)], 950),
        "9001": dirs(&[("/work/w", 9)], 900), "9002": dirs(&[("/third/t", 8)], 900)}));
    let hosts = json!([row("fixture", json!([disk(&root, 90.0), work]))]);
    fx.run(hosts.clone());
    fx.run(hosts);
    let queued = fx.queued();
    assert_eq!(queued.len(), 3, "{queued:?}");
    let roots: Vec<_> = queued
        .iter()
        .filter(|k| **k == measured_key("fixture", &root))
        .collect();
    assert_eq!(roots.len(), 1, "{queued:?}");
    let all = fs::read_to_string(fx.state().join("queue.json")).unwrap() + &fx.active();
    assert!(!all.contains("9002") && !all.contains("/third"), "{all}");
    assert!(all.contains("/root/r") && all.contains("/work/w"));
}

#[test]
fn missing_work_sample_preserves_only_its_crossing() {
    let fx = Fx::new();
    let both = json!([disk("11", 90.0), disk("22", 91.0)]);
    let active = fx.run(json!([row("fixture", both)]));
    assert_eq!(
        sorted(active.lines().map(str::to_owned).collect()),
        [bare("fixture", "11"), bare("fixture", "22")]
    );
    // Root is sampled cool and the work mount is not sampled at all.
    let active = fx.run(json!([row("fixture", json!([disk("11", 40.0)]))]));
    assert_eq!(active, bare("fixture", "22"));
    // A snapshot without the host keeps the work crossing too.
    let active = fx.run(json!([row("other", json!([disk("1", 1.0)]))]));
    assert_eq!(active, bare("fixture", "22"));
    let active = fx.run(json!([row("fixture", json!([disk("22", 85.0)]))]));
    assert_eq!(active, "");
    assert_eq!(fx.queued().len(), 2);
}

#[test]
fn shared_root_and_temp_device_queues_once() {
    let fx = Fx::new();
    let consumers = json!([{"path":"/shared/a","bytes":5}]);
    // Root and temp are one filesystem; a second filesystem is hot beside it.
    fx.run(json!([row(
        "fixture",
        json!([
            disk("7", 90.0),
            measured("7", 91.0, consumers),
            disk("8", 95.0)
        ])
    )]));
    assert_eq!(
        sorted(fx.queued()),
        sorted(vec![measured_key("fixture", "7"), bare("fixture", "8")])
    );
}

#[cfg(unix)]
#[test]
fn legacy_root_and_temp_on_one_local_device_queue_once() {
    let fx = Fx::new().local().set("PLANGRAPH_HOST_TMP_PATH", "/");
    fx.marks(json!({}));
    fx.run(json!([legacy("fixture", 90.0, 91.0, json!([95.0]))]));
    assert_eq!(
        sorted(fx.queued()),
        sorted(vec![
            bare("fixture", &root_device()),
            "host-disk: host=\"fixture\"".into()
        ])
    );
}

#[test]
fn windows_local_watermark_uses_bare_alert() {
    let fx = Fx::new().local();
    // Rows exist for devices the host could name; a legacy work percentage names none of them.
    #[cfg(unix)]
    let root = root_device();
    #[cfg(not(unix))]
    let root = "0".to_owned();
    fx.marks(json!({root.as_str(): dirs(&[("/root/r", 7)], 900), "0": dirs(&[("/zero", 1)], 900)}));
    fx.run(json!([legacy("fixture", 0.0, 0.0, json!([95.0]))]));
    assert_eq!(fx.queued(), ["host-disk: host=\"fixture\""]);
    #[cfg(windows)]
    {
        // No local device lookup: a legacy root percentage stays anonymous too.
        let fx = Fx::new().local();
        fx.marks(json!({"0": dirs(&[("/zero", 1)], 900)}));
        fx.run(json!([legacy("fixture", 0.0, 90.0, json!([]))]));
        assert_eq!(fx.queued(), ["host-disk: host=\"fixture\""]);
    }
    // Collector-supplied consumers on a keyed sample queue the measured alert on every platform.
    let hosts = json!([row(
        "fixture",
        json!([measured("30", 95.0, json!([{"path":"/c","bytes":3}]))])
    )]);
    fx.run(hosts);
    assert_eq!(
        fx.queued(),
        [
            "host-disk: host=\"fixture\"".to_owned(),
            measured_key("fixture", "30")
        ]
    );
}

#[test]
fn a_measured_disk_alert_names_the_top_five_consumers_once() {
    let fx = Fx::new();
    let rows = |bump: u64| -> Value {
        (1..=7)
            .map(|i| json!({"path":format!("/d{i}"),"bytes":i * 10 + bump}))
            .collect()
    };
    fx.run(json!([row(
        "fixture",
        json!([measured("5", 90.0, rows(0))])
    )]));
    let queue = fx.queue();
    assert_eq!(queue.len(), 1);
    assert_eq!(
        queue[0]["message"],
        "FLOW-WATCHDOG: host-disk: host=\"fixture\" fs=\"5\" measured top=/d7=70 /d6=60 /d5=50 /d4=40 /d3=30"
    );
    // The dedupe key holds no sizes: moving sizes do not re-alert.
    fx.run(json!([row(
        "fixture",
        json!([measured("5", 91.0, rows(1))])
    )]));
    assert_eq!(fx.queue().len(), 1);
    // A measurement that disappears neither re-alerts nor ends the crossing.
    let active = fx.run(json!([row("fixture", json!([disk("5", 92.0)]))]));
    assert_eq!(active, measured_key("fixture", "5"));
    assert_eq!(fx.queue().len(), 1);
}

#[cfg(unix)]
#[test]
fn a_root_disk_alert_names_only_the_directories_measured_on_the_root_filesystem() {
    let root = root_device();
    let fx = Fx::new().local();
    fx.marks(
        json!({root.as_str(): dirs(&[("/root/r", 7)], 900), "9001": dirs(&[("/work/w", 9)], 900)}),
    );
    // An anonymous work percentage cannot be attributed: it raises a bare aggregate alert only.
    fx.run(json!([legacy("fixture", 0.0, 90.0, json!([95.0]))]));
    let queue = fx.queue();
    let text = |key: String| {
        queue
            .iter()
            .find(|e| e["active"] == key.as_str())
            .map(|e| e["message"].to_string())
    };
    assert!(text(measured_key("fixture", &root))
        .unwrap()
        .contains("/root/r=7"));
    assert!(!queue
        .iter()
        .any(|e| e["message"].to_string().contains("/work/w")));
    assert!(text("host-disk: host=\"fixture\"".into()).is_some());
    // The work device named explicitly is measured on its own row.
    let fx = Fx::new().local();
    fx.marks(
        json!({root.as_str(): dirs(&[("/root/r", 7)], 900), "9001": dirs(&[("/work/w", 9)], 900)}),
    );
    fx.run(json!([row(
        "fixture",
        json!([disk(&root, 90.0), disk("9001", 95.0)])
    )]));
    for event in fx.queue() {
        let message = event["message"].to_string();
        assert_eq!(
            message.contains("/root/r"),
            event["active"] == measured_key("fixture", &root).as_str()
        );
        assert_eq!(
            message.contains("/work/w"),
            event["active"] == measured_key("fixture", "9001").as_str()
        );
    }
    assert_eq!(fx.queue().len(), 2);
}

#[test]
fn equality_at_85_percent_clears_the_crossing_and_the_next_hot_sample_requeues() {
    let mut fx = Fx::new().at(100);
    for (used, now, active) in [
        (86.0, 100, bare("fixture", "5")),
        (85.0, 200, String::new()),
        (86.0, 300, bare("fixture", "5")),
    ] {
        fx = fx.at(now);
        assert_eq!(
            fx.run(json!([row("fixture", json!([disk("5", used)]))])),
            active
        );
    }
    let queue = fx.queue();
    assert_eq!(queue.len(), 2);
    // The retired event cannot be mistaken for the new crossing: each has its own id.
    assert_eq!(queue[0]["recovered"], true);
    assert_ne!(queue[0]["transition"], queue[1]["transition"]);
    assert_eq!(queue[1]["recovered"], Value::Null);
}

#[cfg(unix)]
#[test]
fn stale_watermark_waits_for_same_device_refresh() {
    let fx = Fx::new().local();
    let hot = || json!([row("fixture", json!([disk("55", 90.0)]))]);
    fx.marks(json!({"55": dirs(&[("/old/o", 1)], 100)}));
    let fx = fx.at(150);
    fx.run(hot());
    assert_eq!(fx.queued(), [measured_key("fixture", "55")]);
    // The watchdog alone sees the recovery at 200.
    let fx = fx.at(200);
    assert_eq!(
        fx.run(json!([row("fixture", json!([disk("55", 40.0)]))])),
        ""
    );
    // The same device is hot again; hygiene's row (100) is older than that recovery: bare only.
    let fx = fx.at(300);
    fx.run(hot());
    assert_eq!(
        fx.queued(),
        [measured_key("fixture", "55"), bare("fixture", "55")]
    );
    // Only a measurement refreshed after the recovery adds the measured alert.
    fx.marks(json!({"55": dirs(&[("/new/n", 2)], 250)}));
    let fx = fx.at(400);
    fx.run(hot());
    assert_eq!(
        fx.queued(),
        [
            measured_key("fixture", "55"),
            bare("fixture", "55"),
            measured_key("fixture", "55")
        ]
    );
    assert!(fx.queue()[2]["message"]
        .as_str()
        .unwrap()
        .contains("/new/n=2"));
}

#[test]
fn a_queued_event_whose_key_never_reached_active_is_not_queued_again() {
    let fx = Fx::new();
    let key = measured_key("fixture", "5");
    fs::create_dir_all(fx.state()).unwrap();
    fs::write(
        fx.state().join("queue.json"),
        json!([{"kind":"alert","recipient":"routing","message":"FLOW-WATCHDOG: queued",
            "active":key,"transition":format!("{key}#1")}])
        .to_string(),
    )
    .unwrap();
    // Crash window: the event is queued, `active` still holds its previous (empty) value.
    let consumers = json!([{"path":"/a","bytes":1}]);
    let active = fx.run(json!([row(
        "fixture",
        json!([measured("5", 90.0, consumers)])
    )]));
    assert_eq!(active, key);
    assert_eq!(fx.queue().len(), 1);
}

#[cfg(unix)]
#[test]
fn restart_with_a_missing_sample_then_a_hot_sample_delivers_the_event_once() {
    let fx = Fx::new();
    let (routing, push) = (
        fx.dir.path().join("routing.log"),
        fx.dir.path().join("push.log"),
    );
    let hook = |path: &std::path::Path| {
        json!(["/bin/sh", "-c", "cat >> \"$1\"; echo >> \"$1\"", "sh", path]).to_string()
    };
    let fx = fx
        .set("PLANGRAPH_WATCHDOG_DELIVERY", hook(&routing))
        .set("PLANGRAPH_FLOW_PUSH", hook(&push));
    let key = measured_key("fixture", "5");
    fs::create_dir_all(fx.state()).unwrap();
    fs::write(
        fx.state().join("queue.json"),
        json!([{"kind":"alert","recipient":"routing","message":"FLOW-WATCHDOG: queued",
            "active":key,"transition":format!("{key}#1")}])
        .to_string(),
    )
    .unwrap();
    let lines = |path: &std::path::Path| {
        fs::read_to_string(path)
            .unwrap_or_default()
            .lines()
            .filter(|l| !l.is_empty())
            .count()
    };
    // The restart finds the host missing from the snapshot: the key is committed before the drain.
    let active = fx.run(json!([row("other", json!([disk("1", 1.0)]))]));
    assert_eq!(active, key);
    assert_eq!((lines(&routing), lines(&push)), (1, 1));
    assert_eq!(fx.queue().len(), 0);
    let consumers = json!([{"path":"/a","bytes":1}]);
    fx.run(json!([row(
        "fixture",
        json!([measured("5", 90.0, consumers)])
    )]));
    assert_eq!((lines(&routing), lines(&push)), (1, 1));
    assert_eq!(fx.queue().len(), 0);
}

#[cfg(unix)]
#[test]
fn aliases_resolve_to_the_canonical_host_for_keys_retention_and_the_local_lookup() {
    let fx = Fx::new();
    let config = fx.dir.path().join("config/plangraph");
    fs::create_dir_all(&config).unwrap();
    fs::write(
        config.join("hosts.json"),
        include_str!("../../../docs/schemas/examples/hosts.example.json"),
    )
    .unwrap();
    let marks = fx.dir.path().join("marks.json").display().to_string();
    let fx = fx.set("PLANGRAPH_DISK_WATERMARKS", marks).at(1000);
    // No PLANGRAPH_HOST_NAME: the registry's `self` entry (`code`) is the local host, and a row
    // labelled with its alias still finds the watermark row.
    fx.marks(json!({"3": dirs(&[("/code/c", 4)], 900)}));
    let active = fx.run(json!([row("devbox", json!([disk("3", 90.0)]))]));
    assert_eq!(active, measured_key("code", "3"));
    // The collector switching to the canonical id is the same crossing.
    let active = fx.run(json!([row("code", json!([disk("3", 90.0)]))]));
    assert_eq!(active, measured_key("code", "3"));
    assert_eq!(fx.queue().len(), 1);
    // A legacy host line written under the alias is read under the canonical id.
    fs::write(
        fx.state().join("active"),
        format!(
            "{}\nhost-code: host=\"devbox\"",
            measured_key("devbox", "3")
        ),
    )
    .unwrap();
    let active = fx.run(json!([row("code", json!([disk("3", 90.0)]))]));
    assert_eq!(active, measured_key("code", "3"));
    assert_eq!(fx.queue().len(), 1);
}

#[test]
fn a_legacy_aggregate_key_migrates_only_when_one_filesystem_is_hot() {
    let fx = Fx::new();
    fs::create_dir_all(fx.state()).unwrap();
    let legacy_line = "host-disk: host=\"fixture\"";
    fs::write(fx.state().join("active"), legacy_line).unwrap();
    // One hot filesystem: the old alert already covered it, so nothing is queued again.
    let active = fx.run(json!([row(
        "fixture",
        json!([disk("7", 90.0), disk("8", 10.0)])
    )]));
    assert_eq!(active, bare("fixture", "7"));
    assert!(fx.queue().is_empty());
    // Two hot filesystems: the line cannot be attributed, it stays until the host is cool.
    let fx = Fx::new();
    fs::create_dir_all(fx.state()).unwrap();
    fs::write(fx.state().join("active"), legacy_line).unwrap();
    let hot = json!([row("fixture", json!([disk("7", 90.0), disk("8", 91.0)]))]);
    let active = fx.run(hot);
    assert_eq!(
        sorted(active.lines().map(str::to_owned).collect()),
        sorted(vec![
            legacy_line.into(),
            bare("fixture", "7"),
            bare("fixture", "8")
        ])
    );
    let active = fx.run(json!([row(
        "fixture",
        json!([disk("7", 10.0), disk("8", 10.0)])
    )]));
    assert_eq!(active, "");
    // A legacy row with no hot sample ends the aggregate.
    let fx = Fx::new();
    fs::create_dir_all(fx.state()).unwrap();
    fs::write(fx.state().join("active"), legacy_line).unwrap();
    assert_eq!(fx.run(json!([legacy("fixture", 1.0, 1.0, json!([]))])), "");
}

#[test]
fn keyed_rows_need_no_legacy_percentages_and_the_keyed_list_is_authoritative() {
    let fx = Fx::new();
    let mut cool = row("fixture", json!([disk("1", 10.0)]));
    cool["root_used_percent"] = 99.into();
    cool["tmp_used_percent"] = 99.into();
    assert_eq!(fx.run(json!([cool])), "");
    let mut bad = legacy("fixture", 1.0, 1.0, json!([]));
    bad.as_object_mut().unwrap().remove("root_used_percent");
    let output = fx.run_in(&fx.state(), &Fx::snapshot(json!([bad])));
    assert!(!output.status.success());
}
