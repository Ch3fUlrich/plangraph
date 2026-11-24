//! The measured steps of a rejoin: sync, install, reaper, floors and legs.
use super::Ssh;
use pg_worker_core::{
    hosts::Os,
    worker_host::{FloorReport, InstallReport, LegReport, ReapReport, SyncReport},
};
use serde_json::Value;
use std::{collections::BTreeMap, io};

/// The host-side code sync; a host may name another unit in `PG_REJOIN_SYNC_UNIT`.
const SYNC_UNIT: &str = "plangraph-host-sync.service";

impl Ssh<'_> {
    pub(super) fn sync(&mut self) -> io::Result<SyncReport> {
        let before = self.refs("HEAD")?;
        let unit = self
            .host
            .env
            .get("PG_REJOIN_SYNC_UNIT")
            .map_or(SYNC_UNIT, String::as_str);
        self.ok(&["systemctl", "--user", "start", unit], 900)?;
        let after = self.refs("HEAD")?;
        let main = self.refs("origin/main")?;
        if let Some((id, head)) = after.iter().find(|(id, head)| main.get(*id) != Some(head)) {
            return Err(io::Error::other(format!(
                "checkout {id} is at {head}, not at origin/main after the sync"
            )));
        }
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

    pub(super) fn install(&mut self) -> io::Result<InstallReport> {
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
        let stale_hooks = self.stale_hooks()?;
        Ok(InstallReport {
            current: output.status.success()
                && row["status"] == "current"
                && stale_hooks.is_empty(),
            detail: if stale_hooks.is_empty() {
                detail
            } else {
                format!("{detail}; stale hooks: {}", stale_hooks.join(", "))
            },
        })
    }

    /// Installed hooks named in the host's `PG_INSTALL_HOOKS` (JSON of name to path) are
    /// compared with the main build's `PG_MAIN_HOOK_SHA256` (JSON of name to sha256).
    fn stale_hooks(&self) -> io::Result<Vec<String>> {
        let Some(raw) = self.host.env.get("PG_INSTALL_HOOKS") else {
            return Ok(Vec::new());
        };
        let paths: BTreeMap<String, String> = serde_json::from_str(raw)
            .map_err(|_| io::Error::other("PG_INSTALL_HOOKS is not a JSON object of paths"))?;
        let main: BTreeMap<String, String> = std::env::var("PG_MAIN_HOOK_SHA256")
            .ok()
            .and_then(|raw| serde_json::from_str(&raw).ok())
            .unwrap_or_default();
        let mut stale = Vec::new();
        for (name, path) in paths {
            let out = self.ok(&["sha256sum", &path], 60)?;
            let sum = String::from_utf8_lossy(&out);
            let sum = sum.split_whitespace().next().unwrap_or_default();
            if main
                .get(&name)
                .is_none_or(|want| !want.eq_ignore_ascii_case(sum))
            {
                stale.push(name);
            }
        }
        Ok(stale)
    }

    /// Run the host's approved reaper (`pg-worktree-sweep --reap`) with the service's
    /// environment. Without an approval and plan it only lists the eligible stale
    /// worktrees, which then keep the host out of placement until routing approves them.
    pub(super) fn reap(&mut self) -> io::Result<ReapReport> {
        const SCRIPT: &str = "set -a; . \"$HOME/.config/plangraph/disk-hygiene.env\"; set +a; \
            exec \"$HOME/.local/bin/pg-worktree-sweep\" --reap --repo \"$PG_REPO\" \
            --root \"$PG_WORKTREES\" --root \"$PG_CLAUDE_WORKTREES\" --root \"$PG_RUNS\" \
            --root \"$PG_RUN_WORKTREES\" --pins \"$PG_PINS\" \"$@\"";
        let rows = |out: &[u8]| {
            String::from_utf8_lossy(out)
                .lines()
                .filter(|line| line.starts_with('{'))
                .count() as u64
        };
        let report = |args: &[&str]| -> io::Result<u64> {
            let mut argv = vec!["bash", "-c", SCRIPT, "pg-worktree-sweep"];
            argv.extend_from_slice(args);
            Ok(rows(&self.ok(&argv, 900)?))
        };
        let before = report(&[])?;
        let approval = self.host.env.get("PG_REJOIN_REAP_APPROVAL");
        let plan = self.host.env.get("PG_REJOIN_REAP_PLAN");
        let (Some(approval), Some(plan)) = (approval, plan) else {
            return Ok(ReapReport {
                removed: 0,
                remaining: before,
            });
        };
        report(&["--approval", approval, "--plan", plan])?;
        let remaining = report(&[])?;
        Ok(ReapReport {
            removed: before.saturating_sub(remaining),
            remaining,
        })
    }

    pub(super) fn floors(&mut self) -> io::Result<FloorReport> {
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
            // A host without work mounts keeps its work on the state disk: measure that.
            _ if self.host.os == Os::Linux && !self.host.state_dir.is_empty() => {
                let out = self.ok(&["df", "-Pk", &self.host.state_dir], 30)?;
                match df_available_kb(&String::from_utf8_lossy(&out)) {
                    Some(kb) if kb >= 5 << 20 => true,
                    Some(kb) => {
                        problems.push(format!(
                            "disk: state disk has {} MiB free < 5 GiB",
                            kb >> 10
                        ));
                        false
                    }
                    None => {
                        unchecked.push("disk: unreadable df output for the state disk".into());
                        true
                    }
                }
            }
            _ => {
                unchecked.push("disk: no work mounts or state disk to measure".into());
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
    pub(super) fn legs(&mut self) -> io::Result<LegReport> {
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

/// Available kibibytes of the last line of `df -Pk`.
fn df_available_kb(output: &str) -> Option<u64> {
    output
        .lines()
        .last()?
        .split_whitespace()
        .nth(3)?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::df_available_kb;

    #[test]
    fn df_reads_the_available_column_of_the_last_line() {
        let out =
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/x 100 40 60 40% /\n";
        assert_eq!(df_available_kb(out), Some(60));
        assert_eq!(df_available_kb("Filesystem\n"), None);
    }
}
