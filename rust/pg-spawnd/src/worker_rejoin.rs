//! `pg-spawnd rejoin HOST`: the onboarding gate of a worker host that was unreachable.
use crate::worker_liveness::{registry, remote, state};
use pg_worker_core::{
    host_command,
    hosts::HostEntry,
    worker_host::{
        FloorReport, InstallReport, LegReport, ReapReport, RejoinHost, RunFate, Store, SyncReport,
    },
};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{self, Write},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

#[path = "worker_rejoin_checks.rs"]
mod checks;
#[path = "worker_rejoin_runs.rs"]
mod runs;

struct Ssh<'a> {
    host: &'a HostEntry,
    state: std::path::PathBuf,
    /// The host's own view of its runs, read once per rejoin.
    inventory: Option<Inventory>,
}

/// The host's run records with each session checked (see `worker-active-runs`).
#[derive(Default)]
struct Inventory {
    live: BTreeSet<String>,
    queued: BTreeSet<String>,
    unverified: BTreeSet<String>,
    dead: BTreeSet<String>,
    terminal: BTreeMap<String, Value>,
}

impl Ssh<'_> {
    fn run(&self, argv: &[&str], timeout: u64) -> io::Result<std::process::Output> {
        let mut command: Command = remote(self.host).map_err(io::Error::other)?;
        // ssh joins the words and the remote side splits them again: keep each word whole.
        let wsl = self.host.wsl_distro.is_some();
        command.args(argv.iter().map(|word| quote_word(word, wsl)));
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

    /// One ref of every registered checkout; a checkout that cannot be read blocks the gate.
    fn refs(&self, rev: &str) -> io::Result<BTreeMap<String, String>> {
        self.host
            .repos
            .iter()
            .map(|(id, repo)| {
                let out = self.ok(&["git", "-C", &repo.root, "rev-parse", rev], 30)?;
                Ok((id.clone(), String::from_utf8_lossy(&out).trim().to_owned()))
            })
            .collect()
    }
}

/// A word the remote side keeps whole: single quotes for a POSIX shell, double quotes
/// for the Windows command line that starts `wsl.exe`. Plain words pass unchanged.
fn quote_word(word: &str, wsl: bool) -> String {
    let plain = !word.is_empty()
        && word
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_./:=@%+,-".contains(&b));
    if plain {
        word.to_owned()
    } else if wsl {
        format!("\"{}\"", word.replace('"', "\\\""))
    } else {
        format!("'{}'", word.replace('\'', "'\\''"))
    }
}

fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs())
}

impl RejoinHost for Ssh<'_> {
    fn sync_checkouts(&mut self) -> io::Result<SyncReport> {
        self.sync()
    }
    fn check_install(&mut self) -> io::Result<InstallReport> {
        self.install()
    }
    fn classify_run(&mut self, run: &str) -> io::Result<RunFate> {
        self.classify(run)
    }
    fn settle_run(&mut self, run: &str, fate: RunFate) -> io::Result<()> {
        self.settle(run, fate)
    }
    fn reap_worktrees(&mut self) -> io::Result<ReapReport> {
        self.reap()
    }
    fn check_floors(&mut self) -> io::Result<FloorReport> {
        self.floors()
    }
    fn probe_legs(&mut self) -> io::Result<LegReport> {
        self.legs()
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
        inventory: None,
    };
    let record = store
        .rejoin(&mut host, now())
        .map_err(|error| error.to_string())?;
    writeln!(out, "{record}").map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::quote_word;

    #[test]
    fn words_with_spaces_or_quotes_stay_whole_across_the_remote_shell() {
        assert_eq!(quote_word("pg-spawnd", false), "pg-spawnd");
        assert_eq!(
            quote_word("/tmp/a plan.ndjson", false),
            "'/tmp/a plan.ndjson'"
        );
        assert_eq!(quote_word("it's", false), "'it'\\''s'");
        assert_eq!(quote_word("D:/a plan", true), "\"D:/a plan\"");
    }
}
