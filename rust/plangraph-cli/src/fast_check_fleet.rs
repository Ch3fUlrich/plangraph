//! `plangraph fast-checks --dispatch`: run a command on the best eligible registry host.
//!
//! plangraph fast-checks --dispatch --registry FILE --repo ID [--hosts a,b] [--log FILE] -- CMD...
//! plangraph fast-checks --dispatch --report LOG
//!
//! Capacity comes from the existing probe: this host reads it directly, other hosts
//! answer `pg-spawnd probe --json`. The memory floor is the registry's `mem_floor_mb`.
use crate::fast_check_dispatch::{dispatch, summarize, Candidate, Fleet, Outcome, Sample, Verdict};
use pg_worker_core::host_command;
use pg_worker_core::host_probe::{self, HostProbe};
use pg_worker_core::hosts::HostRegistry;
use std::io::Write;
use std::path::Path;
use std::process::Command;

struct Machines {
    command: Vec<String>,
    local: String,
}

fn words(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

/// `ssh` to the host's route (validated, option-safe) running `argv`; a WSL host goes
/// through `wsl.exe --distribution D --exec`, in `cwd` when `in_cwd`.
fn remote_command(host: &Candidate, argv: &[String], in_cwd: bool) -> Result<Command, String> {
    let route = host.route.as_deref().ok_or("host has no SSH route")?;
    let mut command = host_command::ssh(route).map_err(|_| "invalid SSH route".to_string())?;
    if let Some(distro) = host.wsl_distro.as_deref() {
        if !host_command::valid_wsl_distro(distro) {
            return Err("invalid WSL distribution".into());
        }
        command.args(["wsl.exe", "--distribution", distro]);
        if in_cwd {
            command.arg("--cd").arg(windows_word(&host.cwd)?);
        }
        command.arg("--exec");
        for word in argv {
            command.arg(windows_word(word)?);
        }
    } else {
        let line: Vec<String> = argv.iter().map(|w| quote(w)).collect();
        let line = line.join(" ");
        command.arg(if in_cwd {
            format!("cd {} && {line}", quote(&host.cwd))
        } else {
            line
        });
    }
    Ok(command)
}

/// One word for the remote Windows shell (cmd or PowerShell), which ssh joins with spaces:
/// plain words pass, words with spaces are double-quoted, anything the shells treat as
/// syntax is refused rather than escaped differently per shell.
fn windows_word(word: &str) -> Result<String, String> {
    let plain = |c: char| c.is_ascii_alphanumeric() || "_-./:=@+,~\\".contains(c);
    if word.is_empty() || !word.chars().all(|c| plain(c) || c == ' ') {
        return Err(format!("unsafe for the Windows shell: {word:?}"));
    }
    Ok(if word.contains(' ') {
        format!("\"{word}\"")
    } else {
        word.to_owned()
    })
}

fn quote(word: &str) -> String {
    format!("'{}'", word.replace('\'', "'\\''"))
}

impl Fleet for Machines {
    fn sample(&mut self, host: &Candidate) -> Option<Sample> {
        let probe: HostProbe = match &host.route {
            None => host_probe::probe(&self.local).ok()?,
            Some(_) => {
                let probe = words(&["pg-spawnd", "probe", "--json", "--host-id", &host.name]);
                let out = remote_command(host, &probe, false).ok()?.output().ok()?;
                out.status
                    .success()
                    .then(|| serde_json::from_slice(&out.stdout).ok())
                    .flatten()?
            }
        };
        Some(Sample {
            mem_available_mb: probe.mem_available_mb,
            cpus: probe.cpus,
            load1: probe.load1,
        })
    }

    fn run(&mut self, host: &Candidate) -> Outcome {
        let mut command = match &host.route {
            None => {
                let mut local = Command::new(&self.command[0]);
                local.args(&self.command[1..]).current_dir(&host.cwd);
                local
            }
            Some(_) => match remote_command(host, &self.command, true) {
                Ok(ssh) => ssh,
                Err(error) => {
                    return Outcome {
                        code: 1,
                        output: format!("{}: {error}\n", host.name),
                    }
                }
            },
        };
        match command.output() {
            Ok(out) => Outcome {
                code: out.status.code().unwrap_or(1),
                output: format!(
                    "{}{}",
                    String::from_utf8_lossy(&out.stdout),
                    String::from_utf8_lossy(&out.stderr)
                ),
            },
            Err(error) => Outcome {
                code: 1,
                output: format!("unable to start {}: {error}\n", self.command[0]),
            },
        }
    }
}

fn candidates(
    registry: &HostRegistry,
    repo: &str,
    only: &[String],
) -> Result<Vec<Candidate>, String> {
    let me = registry.self_host().id.clone();
    let found: Vec<Candidate> = registry
        .hosts
        .iter()
        .filter(|h| !h.inventory_only() && (only.is_empty() || only.contains(&h.id)))
        .filter_map(|h| {
            let route = if h.id == me {
                None
            } else {
                Some(h.ssh_route()?)
            };
            Some(Candidate {
                name: h.id.clone(),
                route,
                wsl_distro: h.wsl_distro.clone(),
                cwd: h.repo(repo)?.root.clone(),
                mem_floor_mb: h.caps.mem_floor_mb,
            })
        })
        .collect();
    if found.is_empty() {
        return Err(format!("no registry host carries repo {repo}"));
    }
    Ok(found)
}

fn log_attempts(path: &Path, done: &crate::fast_check_dispatch::Done) -> Result<(), String> {
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    for (index, attempt) in done.attempts.iter().enumerate() {
        let row = serde_json::json!({
            "host": attempt.host, "attempt": index + 1, "verdict": attempt.verdict,
        });
        writeln!(file, "{row}").map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn take(args: &[String], index: &mut usize) -> super::Result<String> {
    *index += 1;
    args.get(*index)
        .cloned()
        .ok_or_else(|| format!("{} needs a value", args[*index - 1]))
}

pub(super) fn run(args: &[String]) -> super::Result<()> {
    let (mut registry, mut repo, mut log, mut report) = (None, None, None, None);
    let mut only: Vec<String> = Vec::new();
    let mut command: Vec<String> = Vec::new();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--registry" => registry = Some(take(args, &mut index)?),
            "--repo" => repo = Some(take(args, &mut index)?),
            "--log" => log = Some(take(args, &mut index)?),
            "--report" => report = Some(take(args, &mut index)?),
            "--hosts" => {
                only = take(args, &mut index)?
                    .split(',')
                    .map(str::to_owned)
                    .collect()
            }
            "--" => {
                command = args[index + 1..].to_vec();
                break;
            }
            flag => return Err(format!("unknown dispatch argument {flag}")),
        }
        index += 1;
    }
    if let Some(path) = report {
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{path}: {e}"))?;
        println!(
            "{}",
            serde_json::to_string(&summarize(&text)).map_err(|e| e.to_string())?
        );
        return Ok(());
    }
    if command.is_empty() {
        return Err("--dispatch needs a command after --".into());
    }
    let registry = HostRegistry::load(Path::new(
        &registry.ok_or("--dispatch needs --registry FILE")?,
    ))
    .map_err(|e| e.to_string())?;
    let hosts = candidates(&registry, &repo.ok_or("--dispatch needs --repo ID")?, &only)?;
    let mut fleet = Machines {
        command,
        local: registry.self_host().id.clone(),
    };
    let done = dispatch(&mut fleet, &hosts)?;
    print!("{}", done.output);
    if let Some(path) = log {
        log_attempts(Path::new(&path), &done)?;
    }
    for attempt in &done.attempts {
        eprintln!(
            "fast-checks dispatch: {} -> {:?}",
            attempt.host, attempt.verdict
        );
    }
    match done.verdict {
        Verdict::Pass => Ok(()),
        verdict => Err(verdict_error(verdict, done.code)),
    }
}

fn verdict_error(verdict: Verdict, code: i32) -> String {
    match verdict {
        Verdict::HostBusy => {
            "host_busy: the busy-host retry was exhausted (one retry on another eligible host)"
                .into()
        }
        _ => format!("failed with exit {code}"),
    }
}

#[cfg(test)]
#[path = "fast_check_fleet_tests.rs"]
mod tests;
