use super::adapters::{output, python, quota_snapshots, Env, Keeper, Transport};
use super::*;
use pg_worker_core::work_disks;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
pub(super) fn read_json(path: &Path) -> Result<Value, String> {
    serde_json::from_str(&fs::read_to_string(path).map_err(|_| "JSON file unreadable")?)
        .map_err(|_| "invalid JSON file".into())
}
pub(super) fn registry_policy(home: &Path, p: &mut Policy) -> Result<Option<String>, String> {
    let path = home.join(".config/plangraph/hosts.json");
    let r = match read_json(&path) {
        Ok(r) => r,
        Err(_) if !path.exists() => return Ok(None),
        Err(e) => return Err(e),
    };
    for host in r["hosts"].as_array().ok_or("invalid host registry")? {
        let id = host["id"]
            .as_str()
            .ok_or("invalid registry host id")?
            .to_ascii_lowercase();
        p.host_aliases.entry(id.clone()).or_insert(id.clone());
        if let Some(aliases) = host["aliases"].as_array() {
            for a in aliases {
                p.host_aliases
                    .entry(
                        a.as_str()
                            .ok_or("invalid registry alias")?
                            .to_ascii_lowercase(),
                    )
                    .or_insert(id.clone());
            }
        }
    }
    Ok(r["self"].as_str().map(str::to_string))
}
fn associations(state: &Path, runs: &[Run], dry: bool) -> Result<Vec<Value>, String> {
    let mut found = vec![];
    let entries = match fs::read_dir(state) {
        Ok(e) => e,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(found),
        Err(_) => return Err("cannot read launch associations".into()),
    };
    for e in entries {
        let e = e.map_err(|_| "cannot read launch association")?;
        if e.file_name().to_string_lossy().starts_with("launch-") {
            let path = e.path().join("receipt.json");
            if path.exists() {
                if let Ok(receipt) = read_json(&path) {
                    let terminal = runs
                        .iter()
                        .any(|r| !r.active() && receipt["run_id"].as_str() == Some(&r.run_id));
                    if terminal {
                        if !dry {
                            let _ = fs::remove_file(&path);
                        }
                    } else {
                        found.push(receipt);
                    }
                }
            }
        }
    }
    Ok(found)
}
// Older run producers stored the identity only in the launch card.
fn read_runs(data: &Value) -> Result<Vec<Run>, String> {
    let rows = data.get("runs").ok_or("ps missing runs")?;
    let mut runs: Vec<Run> =
        serde_json::from_value(rows.clone()).map_err(|_| "invalid runs JSON")?;
    for (run, row) in runs.iter_mut().zip(rows.as_array().expect("decoded runs")) {
        if run.item_key.is_empty() {
            run.item_key = row["card"]["item_key"].as_str().unwrap_or("").into();
        }
    }
    Ok(runs)
}
fn decode_runs(
    data: &Value,
    ready: &Value,
    snapshot: &Value,
    receipts: &[Value],
) -> Result<Vec<Run>, String> {
    let mut runs = read_runs(data)?;
    for run in runs.iter_mut().filter(|r| r.active()) {
        if let Some(receipt) = receipts
            .iter()
            .find(|v| v["run_id"].as_str() == Some(&run.run_id) && !run.run_id.is_empty())
        {
            if run.item_key.is_empty() {
                run.item_key = receipt["item_key"].as_str().unwrap_or("").into();
            }
            if run.host.is_empty() {
                run.host = receipt["host"].as_str().unwrap_or("").into();
            }
            if run.started.is_none() {
                run.started = receipt["started"].as_f64();
            }
        }
        if !run.writer() {
            continue;
        }
        // Unknown scope consumes host capacity through apply_facts; it cannot
        // establish a project/file conflict with every candidate in the fleet.
        if run.item_key.is_empty() {
            continue;
        }
        if run.host.is_empty() {
            return Err("unknown_writer_host".into());
        }
        if !run.item_key.starts_with("plangraph:") {
            continue;
        }
        let item = [ready, snapshot]
            .iter()
            .flat_map(|r| {
                ["items", "in_progress", "needs_reconcile"]
                    .into_iter()
                    .filter_map(move |k| r.get(k).and_then(Value::as_array))
                    .flatten()
            })
            .find(|i| i["key"].as_str() == Some(&run.item_key));
        let item = item.ok_or("writer_item_missing")?;
        run.files = decode_task(item)?.files;
    }
    Ok(runs)
}
pub(super) fn runs(
    env: &Env,
    repo: &Path,
    ready: &Value,
    state: &Path,
    dry: bool,
) -> Result<Vec<Run>, String> {
    let mut cmd = python(env, repo);
    cmd.args(["-m", "plangraph.spawner.cli", "ps"]);
    let data = output(cmd)?;
    // Snapshot is the existing API with full persisted notes, including claimed
    // items. Show omits note; brief only includes its final line and truncates it.
    let raw = read_runs(&data)?;
    let receipts = associations(state, &raw, dry)?;
    let need_snapshot = raw
        .iter()
        .any(|r| r.writer() && (r.item_key.starts_with("plangraph:") || r.item_key.is_empty()));
    let snapshot = if need_snapshot {
        Keeper { env }.call("snapshot?fmt=json", None)?
    } else {
        json!({"items":[]})
    };
    decode_runs(&data, ready, &snapshot, &receipts)
}
#[allow(clippy::too_many_arguments)]
fn apply_facts(
    mut f: HostFacts,
    runs: &[Run],
    p: &Policy,
    now: i64,
    off: bool,
    local: Option<&str>,
    quota: Option<&Value>,
) -> Result<HostFacts, String> {
    if local.is_some_and(|local| !same_host(local, &f.host, p)) {
        return Err("facts_host_mismatch".into());
    }
    f.now = now;
    f.kill_switch = off;
    f.running_writers = runs
        .iter()
        .filter(|r| r.writer() && same_host(&r.host, &f.host, p))
        .count();
    let unmeasured = runs
        .iter()
        .filter(|r| {
            r.writer()
                && same_host(&r.host, &f.host, p)
                && r.started.is_none_or(|at| at >= f.observed_at as f64)
        })
        .count() as u64;
    f.free_ram_bytes = f
        .free_ram_bytes
        .saturating_sub(p.ram_per_run_bytes.saturating_mul(unmeasured));
    if let Some(rows) = quota {
        f.quota_remaining_percent = super::quota::remaining(rows, now, p.quota_max_age_seconds);
    }
    Ok(f)
}
pub(super) fn facts(
    env: &Env,
    repo: &Path,
    f: HostFacts,
    runs: &[Run],
    home: &Path,
    p: &Policy,
    local: Option<&str>,
) -> Result<HostFacts, String> {
    let mut f: HostFacts =
        serde_json::from_value(read_json(path)?).map_err(|_| "invalid host facts")?;
    f.free_disk_bytes = disk_limit(
        f.free_disk_bytes,
        work_disks::worktree_disk_with(
            env.get(work_disks::WORKTREE_MOUNTS).map(OsStr::new),
            env.get("PLANGRAPH_DISK_MIN_FREE_PERCENT").map(OsStr::new),
            work_disks::mounted_space,
        ),
    )?;
    let rows = quota_snapshots(env, repo).ok();
    apply_facts(
        f,
        runs,
        p,
        chrono::Utc::now().timestamp(),
        home.join(".config/plangraph/feeder.off").exists(),
        env.get("PLANGRAPH_HOST_NAME").map(String::as_str).or(local),
        rows.as_ref(),
    )
}

fn disk_limit(
    reported: u64,
    work_disk: std::io::Result<Option<(PathBuf, pg_worker_core::disk_runtime::DiskSpace)>>,
) -> Result<u64, String> {
    let disk = work_disk.map_err(|error| error.to_string())?;
    Ok(disk.map_or(reported, |(_, space)| reported.min(space.free)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn feeder_reads_work_disk_settings_from_its_environment_file() {
        let fixture = tempfile::tempdir().unwrap();
        let mount = fixture.path().join("offline");
        let env_file = fixture.path().join("plangraph.env");
        let snapshot = fixture.path().join("facts.json");
        fs::write(
            &snapshot,
            r#"{"host":"fixture","free_ram_bytes":100,"free_disk_bytes":100,"observed_at":0}"#,
        )
        .unwrap();
        let sample = |percent| {
            fs::write(
                &env_file,
                format!(
                    "PLANGRAPH_WORKTREE_MOUNTS={}\nPLANGRAPH_DISK_MIN_FREE_PERCENT={percent}\n",
                    mount.display()
                ),
            )
            .unwrap();
            let env = crate::env::load(&env_file, std::iter::empty()).unwrap();
            facts(
                &env,
                fixture.path(),
                &snapshot,
                &[],
                fixture.path(),
                &Policy::default(),
                None,
            )
            .expect_err("configured work disk must constrain the host facts")
        };
        assert_eq!(sample("bad"), "disk_low: invalid free percentage");
        let reason = sample("17");
        assert!(reason.starts_with("disk_low: no work mount meets 5 GiB and 17% free"));
        assert!(reason.contains(mount.to_str().unwrap()), "{reason}");
    }

    #[test]
    fn feeder_disk_limit_uses_the_tighter_sample_and_propagates_a_mount_refusal() {
        use pg_worker_core::disk_runtime::DiskSpace;
        assert_eq!(disk_limit(100, Ok(None)).unwrap(), 100);
        let sample = || {
            Ok(Some((
                PathBuf::from("fixture"),
                DiskSpace {
                    free: 80,
                    total: 200,
                },
            )))
        };
        assert_eq!(disk_limit(100, sample()).unwrap(), 80);
        assert_eq!(disk_limit(50, sample()).unwrap(), 50);
        let reason = "disk_low: no eligible work mount";
        assert_eq!(
            disk_limit(100, Err(std::io::Error::other(reason))).unwrap_err(),
            reason
        );
    }
    #[test]
    fn card_identity_and_unknown_scope_only_consume_their_host_slot() {
        let data = json!({"runs":[
            {"run_id":"legacy","host":"coding","role":"implement","status":"running",
             "item_key":null,"card":{"item_key":"plangraph:PATCH-A"}},
            {"run_id":"unknown","host":"dell","role":"implement","status":"running"},
            {"run_id":"explicit","host":"coding","role":"implement","status":"running",
             "item_key":"other:EXPLICIT","card":{"item_key":"plangraph:WRONG"}}
        ]});
        let snapshot = json!({"items":[{"key":"plangraph:PATCH-A",
            "note":"files: [\"src/shared.rs\"]"}]});
        let runs = decode_runs(&data, &json!({"items":[]}), &snapshot, &[]).unwrap();
        assert_eq!(runs[0].item_key, "plangraph:PATCH-A");
        assert_eq!(runs[0].files, ["src/shared.rs"]);
        assert_eq!(runs[2].item_key, "other:EXPLICIT");
        let ready = json!({"items":[{"key":"plangraph:PATCH-B","priority":9},
            {"key":"plangraph:BUILD-A","priority":1}]});
        let p = Policy::default();
        assert_eq!(pick(&ready, &runs, &p).unwrap().key, "plangraph:BUILD-A");
        let f: HostFacts = serde_json::from_value(json!({"host":"dell",
            "free_ram_bytes":12000000000u64,"free_disk_bytes":20000000000u64,
            "quota_remaining_percent":80,"observed_at":1000}))
        .unwrap();
        let local = apply_facts(f.clone(), &runs, &p, 1001, false, None, None).unwrap();
        assert_eq!(local.running_writers, 1);
        assert_eq!(local.free_ram_bytes, 8000000000);
        assert_eq!(admit(&local, &p), Admission::Admit);
        let full = Policy {
            max_writers: BTreeMap::from([("dell".into(), 1)]),
            ..p.clone()
        };
        assert_eq!(
            admit(&local, &full),
            Admission::Refuse("max_writers".into())
        );
        let elsewhere = HostFacts {
            host: "coding".into(),
            ..f
        };
        let remote = apply_facts(elsewhere, &runs, &p, 1001, false, None, None).unwrap();
        assert_eq!(remote.running_writers, 2);
        assert_eq!(admit(&remote, &p), Admission::Admit);
    }
    #[test]
    fn claimed_snapshot_notes_and_queued_associations_restore_scope() {
        let data = json!({"runs":[{"run_id":"run-a","item_key":null,"host":"dell","status":"queued","role":"implement"}]});
        let snapshot = json!({"items":[{"key":"plangraph:PATCH-A","state":"claimed","note":"files: [\"src/shared.rs\"]"}]});
        let receipts = vec![
            json!({"run_id":"run-a","item_key":"plangraph:PATCH-A","files":["src/shared.rs"]}),
        ];
        let rs = decode_runs(&data, &json!({"items":[]}), &snapshot, &receipts).unwrap();
        assert_eq!(rs[0].files, ["src/shared.rs"]);
        assert!(rs[0].writer());
        let unknown = decode_runs(&data, &json!({"items":[]}), &snapshot, &[]).unwrap();
        assert!(unknown[0].item_key.is_empty());
        assert!(unknown[0].writer());
        let ready =
            json!({"items":[{"key":"plangraph:OTHER","note":"files: [\"src/shared.rs\"]"}]});
        assert!(pick(&ready, &rs, &Policy::default()).is_none());
    }
    #[test]
    fn corrupt_receipts_do_not_stall_and_only_live_ticks_prune_terminal_receipts() {
        let d = tempfile::tempdir().unwrap();
        let bad = d.path().join("launch-bad");
        fs::create_dir(&bad).unwrap();
        fs::write(bad.join("receipt.json"), "{").unwrap();
        let done = d.path().join("launch-done");
        fs::create_dir(&done).unwrap();
        crate::atomic::write(
            &done.join("receipt.json"),
            br#"{"run_id":"run-done","item_key":"p:A"}"#,
        )
        .unwrap();
        let rs = vec![Run {
            run_id: "run-done".into(),
            status: "completed".into(),
            ..Run::default()
        }];
        assert!(associations(d.path(), &rs, true).unwrap().is_empty());
        assert!(done.join("receipt.json").exists());
        assert!(associations(d.path(), &rs, false).unwrap().is_empty());
        assert!(!done.join("receipt.json").exists());
        assert!(bad.join("receipt.json").exists());
    }
    #[test]
    fn finished_writers_without_identity_do_not_consume_capacity_or_ram() {
        let p = Policy::default();
        let f: HostFacts = serde_json::from_value(json!({"host":"dell",
            "free_ram_bytes":4000000000u64,"free_disk_bytes":20000000000u64,
            "quota_remaining_percent":80,"observed_at":1000}))
        .unwrap();
        let runs: Vec<Run> = ["stalled", "stalled_no_leg", "terminated_after_answer"]
            .into_iter()
            .map(|status| {
                serde_json::from_value(json!({"run_id":status,
                "host":"dell","role":"implement","status":status,
                "item_key":null,"started":null}))
                .unwrap()
            })
            .collect();
        let actual = apply_facts(f, &runs, &p, 1001, false, None, None).unwrap();
        assert_eq!(actual.running_writers, 0);
        assert_eq!(actual.free_ram_bytes, 4000000000);
        assert_eq!(admit(&actual, &p), Admission::Admit);
        assert!(pick(&json!({"items":[{"key":"p:A"}]}), &runs, &p).is_some());
    }
    #[test]
    fn reused_ram_sample_reserves_every_post_sample_writer() {
        let p = Policy::default();
        let f: HostFacts =
            serde_json::from_value(json!({"host":"dell","free_ram_bytes":8000000000u64,
            "free_disk_bytes":20000000000u64,"quota_remaining_percent":80,"observed_at":1000}))
            .unwrap();
        let a = Run {
            host: "dell".into(),
            item_key: "p:A".into(),
            run_id: "run-a".into(),
            role: "implement".into(),
            status: "running".into(),
            started: Some(1001.0),
            ..Run::default()
        };
        let one = apply_facts(
            f.clone(),
            std::slice::from_ref(&a),
            &p,
            1120,
            false,
            None,
            None,
        )
        .unwrap();
        assert_eq!(one.free_ram_bytes, 4000000000);
        assert_eq!(admit(&one, &p), Admission::Admit);
        let b = Run {
            run_id: "run-b".into(),
            started: Some(1121.0),
            ..a.clone()
        };
        let two = apply_facts(
            f.clone(),
            &[a.clone(), b.clone()],
            &p,
            1240,
            false,
            None,
            None,
        )
        .unwrap();
        assert_eq!(two.free_ram_bytes, 0);
        assert_eq!(admit(&two, &p), Admission::Refuse("ram".into()));
        let refreshed = HostFacts {
            observed_at: 1240,
            free_ram_bytes: 4000000000,
            ..f
        };
        let fresh = apply_facts(refreshed, &[a, b], &p, 1241, false, None, None).unwrap();
        assert_eq!(admit(&fresh, &p), Admission::Admit);
    }
    #[test]
    fn facts_use_real_flags_counts_aliases_and_quota_freshness() {
        let mut p = Policy::default();
        p.host_aliases.insert("box.example".into(), "dell".into());
        let f:HostFacts=serde_json::from_value(json!({"host":"dell","free_ram_bytes":8000000000u64,"free_disk_bytes":20000000000u64,
            "quota_remaining_percent":80,"observed_at":1000,"running_writers":0,"kill_switch":false})).unwrap();
        let r = Run {
            host: "box.example".into(),
            status: "running".into(),
            role: "implement".into(),
            ..Run::default()
        };
        let actual =
            apply_facts(f.clone(), &[r], &p, 1001, true, Some("box.example"), None).unwrap();
        assert_eq!(actual.running_writers, 1);
        assert_eq!(admit(&actual, &p), Admission::Refuse("kill_switch".into()));
        assert!(apply_facts(f.clone(), &[], &p, 1001, false, Some("other"), None).is_err());
        let stale = json!([{"provider":"codex","connection_id":"a","window_key":"day","created_at":1,"remaining_percentage":99}]);
        p.quota_max_age_seconds = 100;
        let actual = apply_facts(f, &[], &p, 1001, false, None, Some(&stale)).unwrap();
        assert_eq!(
            admit(&actual, &p),
            Admission::Refuse("provider_quota".into())
        );
    }
}
