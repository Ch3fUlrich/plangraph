//! `pg-spawnd rejoin HOST`: the onboarding gate of a worker host that was unreachable.
use crate::worker_liveness::{registry, remote, state};
use pg_worker_core::{
    host_command,
    hosts::{HostEntry, Os},
    run::record::{RunRecord, Status},
    worker_host::{FloorReport, InstallReport, LegReport, RejoinHost, RunFate, Store, SyncReport},
};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, Write},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

/// The host-side code sync; a host may name another unit in `PG_REJOIN_SYNC_UNIT`.
const SYNC_UNIT: &str = "plangraph-host-sync.service";
const SWEEP_UNIT: &str = "pg-worktree-sweep.service";

struct Ssh<'a> {
    host: &'a HostEntry,
    state: std::path::PathBuf,
    /// Terminal status the host reported for runs that ended during the outage.
    ended: BTreeMap<String, String>,
    live: Option<Vec<String>>,
}

impl Ssh<'_> {
    fn run(&self, argv: &[&str], timeout: u64) -> io::Result<std::process::Output> {
        let mut command: Command = remote(self.host).map_err(io::Error::other)?;
        command.args(argv);
        host_command::capture(&mut command, Duration::from_secs(timeout))
    }

    fn ok(&self, argv: &[&str], timeout: u64) -> io::Result<Vec<u8>> {
        let output = self.run(argv, timeout)?;
        if !output.status.success() {
            return Err(io::Error::other(format!(
                "{} exited with {}",
                argv.join(" "),
                output.status
            )));
        }
        Ok(output.stdout)
    }

    fn heads(&self) -> BTreeMap<String, String> {
        self.host
            .repos
            .iter()
            .filter_map(|(id, repo)| {
                let out = self
                    .ok(&["git", "-C", &repo.root, "rev-parse", "HEAD"], 30)
                    .ok()?;
                Some((id.clone(), String::from_utf8_lossy(&out).trim().to_owned()))
            })
            .collect()
    }

    fn live_runs(&mut self) -> io::Result<&[String]> {
        if self.live.is_none() {
            let out = self.ok(&["pg-spawnd", "worker-active-runs", &self.host.id], 30)?;
            let receipt: Value = serde_json::from_slice(&out).map_err(io::Error::other)?;
            self.live = Some(
                receipt["runs"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|id| id.as_str().map(str::to_owned))
                    .collect(),
            );
        }
        Ok(self.live.as_deref().unwrap_or_default())
    }

    fn load_ended(&mut self) -> io::Result<()> {
        let mut cursor = 0u64;
        loop {
            let out = self.ok(
                &["pg-spawnd", "runs", "--changed-since", &cursor.to_string()],
                60,
            )?;
            let page: Value = serde_json::from_slice(&out).map_err(io::Error::other)?;
            for row in page["rows"].as_array().into_iter().flatten() {
                if let (Some(id), Some(status)) = (row["run_id"].as_str(), row["status"].as_str()) {
                    if row["phase"] == "ended" {
                        self.ended.insert(id.to_owned(), status.to_owned());
                    }
                }
            }
            let next = page["next_cursor"].as_u64().unwrap_or(cursor);
            if page["more"] != true || next <= cursor {
                return Ok(());
            }
            cursor = next;
        }
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

impl RejoinHost for Ssh<'_> {
    fn sync_checkouts(&mut self) -> io::Result<SyncReport> {
        let before = self.heads();
        let unit = self
            .host
            .env
            .get("PG_REJOIN_SYNC_UNIT")
            .map_or(SYNC_UNIT, String::as_str);
        self.ok(&["systemctl", "--user", "start", unit], 900)?;
        let after = self.heads();
        Ok(SyncReport {
            head: after.values().next().cloned().unwrap_or_default(),
            fast_forwarded: after
                .iter()
                .filter(|(id, head)| before.get(*id) != Some(head))
                .map(|(id, _)| id.clone())
                .collect(),
            saved_branches: Vec::new(),
        })
    }

    fn check_install(&mut self) -> io::Result<InstallReport> {
        let env = |key: &str| std::env::var(key).ok().filter(|v| !v.is_empty());
        let (Some(sha), Some(sum), Some(binary), Some(stamp)) = (
            env("PG_MAIN_SHA"),
            env("PG_MAIN_SHA256"),
            self.host.env.get("PG_INSTALL_BINARY"),
            self.host.env.get("PG_INSTALL_STAMP"),
        ) else {
            return Ok(InstallReport {
                current: false,
                detail: "main build identity or install paths not supplied".into(),
            });
        };
        let output = self.run(
            &[
                "pg-spawnd",
                "check-install",
                "--host-id",
                &self.host.id,
                "--binary",
                binary,
                "--source-stamp",
                stamp,
                "--main-sha",
                &sha,
                "--main-sha256",
                &sum,
            ],
            60,
        )?;
        // check-install exits 0 for any valid invocation and names the verdict on stdout.
        let row: Value = serde_json::from_slice(&output.stdout).unwrap_or(Value::Null);
        let detail = row["reason"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| {
                String::from_utf8_lossy(&output.stderr)
                    .lines()
                    .next()
                    .unwrap_or("pg-spawnd or hooks differ from main")
                    .to_owned()
            });
        Ok(InstallReport {
            current: output.status.success() && row["status"] == "current",
            detail,
        })
    }

    fn classify_run(&mut self, run: &str) -> io::Result<RunFate> {
        if self.live_runs()?.iter().any(|id| id == run) {
            return Ok(RunFate::Live);
        }
        if self.ended.is_empty() {
            self.load_ended()?;
        }
        Ok(if self.ended.contains_key(run) {
            RunFate::Finished
        } else {
            RunFate::Dead
        })
    }

    fn settle_run(&mut self, run: &str, fate: RunFate) -> io::Result<()> {
        let mut record = match RunRecord::read(&self.state, run) {
            Ok(record) => record,
            // The run never reached this coordinator's records.
            Err(pg_worker_core::run::record::RecordError::Unknown) => return Ok(()),
            Err(error) => return Err(io::Error::other(error.to_string())),
        };
        let was_unknown = record.get("status").and_then(Value::as_str) == Some("unknown");
        if was_unknown {
            record.set("status", json!("running"));
        }
        if fate == RunFate::Live {
            // Re-attach: the record goes back to running and the host's own reconcile
            // loop keeps following the session.
            if was_unknown {
                let at = now();
                return record
                    .push_event(json!({"event": "REJOIN_REATTACHED", "at": at}))
                    .and_then(|()| record.save())
                    .map_err(|error| io::Error::other(error.to_string()));
            }
            return Ok(());
        }
        if record.status().is_none_or(Status::is_terminal) {
            return Ok(());
        }
        // A queued run walks queued -> running first; only the statuses that can end a
        // running run are kept, and refused, limited or dry-run ends count as failed.
        if record.status() == Some(Status::Queued) {
            record.set("status", json!("running"));
        }
        let to = match fate {
            RunFate::Finished => self
                .ended
                .get(run)
                .and_then(|status| Status::parse(status))
                .filter(|status| {
                    !matches!(
                        status,
                        Status::Queued
                            | Status::Running
                            | Status::Refused
                            | Status::Limited
                            | Status::DryRun
                    )
                })
                .unwrap_or(Status::Failed),
            _ => Status::Failed,
        };
        let at = now();
        record
            .transition(to, at as f64)
            .and_then(|()| {
                record.push_event(json!({"event": "REJOIN_SETTLED", "fate": fate, "at": at}))
            })
            .and_then(|()| record.save())
            .map_err(|error| io::Error::other(error.to_string()))
    }

    /// The sweep unit reports removal candidates only; nothing is deleted here.
    fn sweep_worktrees(&mut self) -> io::Result<()> {
        self.ok(&["systemctl", "--user", "start", SWEEP_UNIT], 600)
            .map(drop)
    }

    fn check_floors(&mut self) -> io::Result<FloorReport> {
        let mut problems = Vec::new();
        let mut unchecked = Vec::new();
        // The host's registry mounts reach the command; an ssh session has no such env.
        let disk_ok = match self.host.env.get("PLANGRAPH_WORK_MOUNTS") {
            Some(mounts) if !mounts.is_empty() => {
                let assign = format!("PLANGRAPH_WORKTREE_MOUNTS={mounts}");
                let disk = self.run(&["env", &assign, "pg-spawnd", "host", "work-disk"], 60)?;
                if !disk.status.success() {
                    problems.push(format!(
                        "disk: {}",
                        String::from_utf8_lossy(&disk.stderr).trim()
                    ));
                }
                disk.status.success()
            }
            _ => {
                unchecked.push("disk: no work mounts configured in the host registry".into());
                true
            }
        };
        let floor = self.host.caps.mem_floor_mb;
        let ram_ok = if self.host.wsl_distro.is_some() {
            // The guest's MemAvailable counts page cache as used; Windows free memory
            // is what starves the host, so ask the WSL cache guard for it.
            let min = floor.max(1).to_string();
            let guard = self.run(
                &[
                    "pg-spawnd",
                    "host",
                    "wsl-cache-guard",
                    "--min-free-mb",
                    &min,
                ],
                120,
            )?;
            let line = String::from_utf8_lossy(&guard.stdout).into_owned();
            let value = |key: &str| {
                line.split_whitespace()
                    .find_map(|word| word.strip_prefix(key)?.parse::<u64>().ok())
            };
            if !guard.status.success() {
                problems.push(format!(
                    "ram: Windows free memory is below {floor} MiB and the page cache could not be dropped"
                ));
                false
            } else if line.contains("status=skipped") {
                unchecked.push("ram: Windows free memory unavailable from the WSL guest".into());
                true
            } else if let Some(free) = value("after_free_mb=").or_else(|| value("before_free_mb="))
            {
                if free < floor {
                    problems.push(format!("ram: Windows free {free} MiB < floor {floor} MiB"));
                }
                free >= floor
            } else {
                unchecked.push("ram: unreadable WSL cache guard output".into());
                true
            }
        } else if self.host.os == Os::Linux {
            let out = self.ok(
                &["pg-spawnd", "probe", "--host-id", &self.host.id, "--json"],
                60,
            )?;
            let probe: Value = serde_json::from_slice(&out).map_err(io::Error::other)?;
            let free = probe["mem_available_mb"].as_u64().unwrap_or(0);
            if free < floor {
                problems.push(format!("ram: {free} MiB available < floor {floor} MiB"));
            }
            free >= floor
        } else {
            unchecked.push("ram: no memory source for a Windows host".into());
            true
        };
        Ok(FloorReport {
            disk_ok,
            ram_ok,
            detail: if problems.is_empty() {
                "disk and RAM floors hold".into()
            } else {
                problems.join("; ")
            },
            unchecked,
        })
    }

    /// Each leg named in the host's `PG_REJOIN_LEG_PROBES` (a JSON object of leg
    /// name to argv, run on the host) counts as working when its command exits 0.
    fn probe_legs(&mut self) -> io::Result<LegReport> {
        let Some(raw) = self.host.env.get("PG_REJOIN_LEG_PROBES") else {
            return Ok(LegReport::default());
        };
        let legs: BTreeMap<String, Vec<String>> = serde_json::from_str(raw)
            .map_err(|_| io::Error::other("PG_REJOIN_LEG_PROBES is not a JSON object of argv"))?;
        let mut report = LegReport::default();
        for (leg, argv) in legs {
            let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
            let up = !argv.is_empty() && self.run(&argv, 180).is_ok_and(|o| o.status.success());
            if up {
                &mut report.working
            } else {
                &mut report.failed
            }
            .push(leg);
        }
        Ok(report)
    }
}

pub(crate) fn execute(args: &[String], out: &mut dyn Write) -> Result<(), String> {
    let [name] = args else {
        return Err("usage: pg-spawnd rejoin HOST".into());
    };
    let registry = registry()?;
    let entry = registry.resolve(name).ok_or("unknown worker host")?;
    let state = state()?;
    let store = Store::new(&state, &entry.id).map_err(|error| error.to_string())?;
    let mut host = Ssh {
        host: entry,
        state,
        ended: BTreeMap::new(),
        live: None,
    };
    let record = store
        .rejoin(&mut host, now())
        .map_err(|error| error.to_string())?;
    writeln!(out, "{record}").map_err(|error| error.to_string())
}
