//! `host-sync --install --fleet`: the per-host install run on every host of the hosts registry, from the
//! center, in parallel. There is no per-host code path: every host, the local one included, runs the same
//! `host-sync --install --machine` (built from the host's checkout when the binary is missing), and the
//! center only reads what it prints. An unreachable or failing host is a named row, never a skip.
use super::ledger::{self, Ledger};
use super::systemd::Out;
use serde_json::Value;
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Access {
    Local,
    Ssh {
        target: String,
        wsl_distro: Option<String>,
    },
    /// The registry names a transport this command cannot use; the host is a named failure.
    Unsupported(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FleetHost {
    pub id: String,
    pub access: Access,
}

/// The hosts of the registry (`hosts.json`: the list the spawner placement uses).
pub fn hosts_from_registry(text: &str) -> Result<Vec<FleetHost>, String> {
    let registry: Value = serde_json::from_str(text).map_err(|e| format!("hosts registry: {e}"))?;
    let hosts = registry["hosts"]
        .as_array()
        .ok_or("hosts registry has no hosts list")?;
    hosts
        .iter()
        .map(|host| {
            let id = host["id"]
                .as_str()
                .ok_or("a registry host has no id")?
                .to_owned();
            let access = match host["transport"].as_str() {
                Some("local") => Access::Local,
                Some("ssh") => match host["ssh_target"].as_str().filter(|t| !t.is_empty()) {
                    Some(target) => Access::Ssh {
                        target: target.to_owned(),
                        wsl_distro: host["wsl_distro"].as_str().map(str::to_owned),
                    },
                    None => Access::Unsupported("ssh host without ssh_target".into()),
                },
                other => Access::Unsupported(format!(
                    "transport {} has no install path",
                    other.unwrap_or("?")
                )),
            };
            Ok(FleetHost { id, access })
        })
        .collect()
}

/// What every host runs: the installed `host-sync`, or the one built from the host's checkout.
pub fn remote_script(check: bool) -> String {
    let flag = if check { " --check" } else { "" };
    format!(
        "if command -v host-sync >/dev/null 2>&1; then exec host-sync --install{flag} --machine; \
         else cd \"${{PLANGRAPH_REPO:-$HOME/code/plangraph}}/rust\" && \
         exec cargo run --release --locked -q -p plangraph-cli --bin host-sync -- --install{flag} --machine; fi"
    )
}

fn base64(bytes: &[u8]) -> String {
    // The standard alphabet from its ranges, so no 64-character literal sits in the source.
    let alphabet: Vec<u8> = (b'A'..=b'Z')
        .chain(b'a'..=b'z')
        .chain(b'0'..=b'9')
        .chain(*b"+/")
        .collect();
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |n, (i, b)| n | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(alphabet[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// The command line that runs `script` on `host`. The script travels base64-encoded, so no quoting of it
/// depends on the remote shell (a Windows ssh server, a WSL login shell).
pub fn argv(host: &FleetHost, script: &str) -> Option<Vec<String>> {
    let wrapped = format!(
        "sh -lc \"echo {} | base64 -d | sh\"",
        base64(script.as_bytes())
    );
    let own = |parts: &[&str]| parts.iter().map(|p| (*p).to_owned()).collect::<Vec<_>>();
    match &host.access {
        Access::Local => Some(own(&[
            "sh",
            "-lc",
            &format!("echo {} | base64 -d | sh", base64(script.as_bytes())),
        ])),
        Access::Ssh { target, wsl_distro } => {
            let mut v = own(&[
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                target,
            ]);
            v.push(match wsl_distro {
                Some(d) => format!("wsl.exe --distribution {d} --exec {wrapped}"),
                None => wrapped,
            });
            Some(v)
        }
        Access::Unsupported(_) => None,
    }
}

/// Runs a command line somewhere; `Err` means it could not be started at all.
pub trait Remote: Sync {
    fn run(&self, host: &FleetHost, argv: &[String]) -> Result<Out, String>;
}

/// Runs the command lines as real processes.
pub struct Processes;

impl Remote for Processes {
    fn run(&self, _: &FleetHost, argv: &[String]) -> Result<Out, String> {
        let output = std::process::Command::new(&argv[0])
            .args(&argv[1..])
            .output()
            .map_err(|e| format!("{}: {e}", argv[0]))?;
        Ok(Out {
            code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        })
    }
}

/// One host's result.
#[derive(Debug)]
pub struct HostResult {
    pub id: String,
    /// The table lines the host printed.
    pub lines: Vec<String>,
    pub pending: bool,
    pub failure: Option<String>,
    pub ledger: Option<Ledger>,
}

fn collect(host: &FleetHost, out: &Out) -> HostResult {
    let mut result = HostResult {
        id: host.id.clone(),
        lines: Vec::new(),
        pending: false,
        failure: None,
        ledger: None,
    };
    let mut saw_pending = false;
    for line in out.stdout.lines() {
        if let Some(flag) = line.strip_prefix("pending\t") {
            saw_pending = true;
            result.pending = flag.trim() != "0";
        } else if let Some(json) = line.strip_prefix("ledger\t") {
            result.ledger = serde_json::from_str(json).ok();
        } else {
            result.lines.push(line.to_owned());
        }
    }
    if !saw_pending {
        let tail = out.stderr.lines().last().unwrap_or("no output").trim();
        result.failure = Some(format!(
            "install run gave no result (exit {}): {tail}",
            out.code
        ));
    } else if result.ledger.is_none() {
        // A host that cannot hand its ledger back reported nothing the center can judge.
        result.failure = Some("install run answered without a ledger".to_owned());
    }
    result
}

fn run_host(host: &FleetHost, script: &str, remote: &dyn Remote) -> HostResult {
    let failed = |why: String| HostResult {
        id: host.id.clone(),
        lines: Vec::new(),
        pending: true,
        failure: Some(why),
        ledger: None,
    };
    let Some(argv) = argv(host, script) else {
        let Access::Unsupported(why) = &host.access else {
            unreachable!()
        };
        return failed(why.clone());
    };
    match remote.run(host, &argv) {
        Err(why) => failed(format!("unreachable: {why}")),
        // ssh itself exits 255 when it cannot connect; the install exits 0 or 1.
        Ok(out) if out.code == 255 => failed(format!(
            "unreachable: {}",
            out.stderr.lines().last().unwrap_or("ssh failed").trim()
        )),
        Ok(out) => collect(host, &out),
    }
}

/// Run the install (or `--check`) on every host in parallel. Returns the table lines
/// (`<host><TAB>...`) and whether any host or entry is still missing; the ledger of each host that
/// answered is written to `ledger_dir/<host>.json` on the center.
pub fn run_fleet(
    hosts: &[FleetHost],
    remote: &dyn Remote,
    check: bool,
    ledger_dir: &Path,
) -> (Vec<String>, bool) {
    let (table, failed, _) = run_fleet_results(hosts, remote, check, ledger_dir);
    (table, failed)
}

/// [`run_fleet`] with each host's result, so a caller can tell a host that answered with a ledger from one
/// that did not (the landing guard refuses on the latter instead of reading an older ledger file).
pub fn run_fleet_results(
    hosts: &[FleetHost],
    remote: &dyn Remote,
    check: bool,
    ledger_dir: &Path,
) -> (Vec<String>, bool, Vec<HostResult>) {
    let script = remote_script(check);
    let mut results: Vec<HostResult> = std::thread::scope(|scope| {
        let handles: Vec<_> = hosts
            .iter()
            .map(|h| scope.spawn(|| run_host(h, &script, remote)))
            .collect();
        handles
            .into_iter()
            .map(|h| h.join().expect("host run panicked"))
            .collect()
    });
    let mut table = Vec::new();
    let mut failed = false;
    for result in &mut results {
        for line in &result.lines {
            table.push(format!("{}\t{line}", result.id));
        }
        if let Some(why) = &result.failure {
            table.push(format!("{}\tFAIL\t{why}", result.id));
        } else if result.pending {
            table.push(format!(
                "{}\tPENDING\tsomething is still missing (see the rows above)",
                result.id
            ));
        } else {
            table.push(format!("{}\tOK\tcomplete", result.id));
        }
        failed |= result.pending || result.failure.is_some();
        if let Some(ledger) = &result.ledger {
            if let Err(error) =
                ledger::save(&ledger_dir.join(format!("{}.json", result.id)), ledger)
            {
                table.push(format!("{}\tFAIL\tledger not written: {error}", result.id));
                result.failure = Some(format!("ledger not written: {error}"));
                failed = true;
            }
        }
    }
    (table, failed, results)
}
