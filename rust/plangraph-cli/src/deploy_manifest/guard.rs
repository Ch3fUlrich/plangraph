//! The GO check of the deploy manifest: a train is refused while a manifest entry has been not live for
//! more than 24 hours on any host that reports, unless a dated routing exception covers it.
//!
//! The ledgers are the files `deploy-status` writes, one per host. Exceptions are `deploy/exceptions.toml`,
//! compiled into the verifier like the other pins, so a change under test cannot grant itself one:
//! `[[exception]] entry = "unit:<name>" | "binary:<package>/<bin>", until = "YYYY-MM-DD", decision = "D-<n>",
//! reason = "..."`. An exception ends at the end of its `until` day (UTC).
use super::ledger::{self, Ledger};
use serde::Deserialize;
use std::path::Path;

/// How long an entry may stay not live before the GO check refuses.
pub const LIMIT_HOURS: i64 = 24;
/// A ledger older than this is not evidence of anything: its host stopped running deploy-status.
pub const LEDGER_MAX_AGE_HOURS: i64 = 6;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Exception {
    pub entry: String,
    pub until: String,
    pub decision: String,
    pub reason: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Exceptions {
    #[serde(default)]
    exception: Vec<Exception>,
}

/// Parse and validate the exception file text.
pub fn parse_exceptions(text: &str) -> Result<Vec<Exception>, String> {
    let parsed: Exceptions = toml::from_str(text).map_err(|e| format!("exceptions: {e}"))?;
    for e in &parsed.exception {
        let d = e.decision.strip_prefix("D-").unwrap_or("");
        if d.is_empty() || !d.bytes().all(|b| b.is_ascii_digit()) {
            return Err(format!(
                "exception {}: decision {:?} is not a D-id",
                e.entry, e.decision
            ));
        }
        if until_epoch(&e.until).is_none() {
            return Err(format!(
                "exception {}: until {:?} is not a YYYY-MM-DD date",
                e.entry, e.until
            ));
        }
        if e.reason.trim().is_empty() {
            return Err(format!("exception {}: a reason is required", e.entry));
        }
    }
    Ok(parsed.exception)
}

/// The first second after the `until` day, UTC.
fn until_epoch(until: &str) -> Option<i64> {
    let day = chrono::NaiveDate::parse_from_str(until, "%Y-%m-%d").ok()?;
    Some(day.succ_opt()?.and_hms_opt(0, 0, 0)?.and_utc().timestamp())
}

/// One reason the GO check refuses: a stable code and the detail naming host, entry, state and age.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Finding {
    pub code: &'static str,
    pub detail: String,
}

/// Read every `*.json` ledger of a directory as (host, ledger); an unreadable file is a finding.
pub fn read_ledgers(dir: &Path) -> (Vec<(String, Ledger)>, Vec<Finding>) {
    let (mut ledgers, mut findings) = (Vec::new(), Vec::new());
    let Ok(read) = std::fs::read_dir(dir) else {
        return (ledgers, findings);
    };
    let mut paths: Vec<_> = read.filter_map(Result::ok).map(|e| e.path()).collect();
    paths.sort();
    for path in paths
        .iter()
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
    {
        let host = path
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or_default();
        match std::fs::read(path)
            .ok()
            .and_then(|b| serde_json::from_slice::<Ledger>(&b).ok())
        {
            Some(ledger) => ledgers.push((host, ledger)),
            None => findings.push(Finding {
                code: "deploy_ledger_unreadable",
                detail: format!("{}: not a deploy-status ledger", path.display()),
            }),
        }
    }
    (ledgers, findings)
}

/// What the ledgers must have been computed from: the manifest and the release stamp of the checkout the
/// GO check runs from. A report for older inputs proves nothing about the entries merged since.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pins {
    pub manifest: String,
    pub stamp: String,
}

/// SHA-256 (hex) of the manifest file of `repo`.
pub fn manifest_digest(repo: &Path) -> Option<String> {
    use sha2::{Digest, Sha256};
    let bytes = std::fs::read(repo.join("deploy/manifest.toml")).ok()?;
    Some(format!("{:x}", Sha256::digest(&bytes)))
}

impl Pins {
    pub fn from_repo(repo: &Path) -> Result<Pins, String> {
        let manifest = manifest_digest(repo)
            .ok_or_else(|| format!("{}: no deploy/manifest.toml", repo.display()))?;
        let (stamp, _) = super::stamp::release_stamp(repo)?;
        Ok(Pins { manifest, stamp })
    }
}

/// The hosts registry text: `path` when given, else `$XDG_CONFIG_HOME/plangraph/hosts.json`.
pub fn registry_text(path: Option<&Path>) -> Option<String> {
    let path = match path {
        Some(path) => path.to_path_buf(),
        None => {
            let config = std::env::var_os("XDG_CONFIG_HOME")
                .filter(|v| !v.is_empty())
                .map(std::path::PathBuf::from)
                .or_else(|| std::env::var_os("HOME").map(|h| Path::new(&h).join(".config")))?;
            config.join("plangraph/hosts.json")
        }
    };
    std::fs::read_to_string(path).ok()
}

/// The newest ledger filed under the host's id or an alias.
pub fn ledger_for<'a>(ledgers: &'a [(String, Ledger)], host: &ExpectedHost) -> Option<&'a Ledger> {
    ledgers
        .iter()
        .filter(|(name, _)| host.owns(name))
        .map(|(_, ledger)| ledger)
        .max_by_key(|l| l.written_at)
}

/// A host the GO check expects a fresh ledger from: a registry host the fleet install can reach.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExpectedHost {
    pub id: String,
    pub aliases: Vec<String>,
}

impl ExpectedHost {
    fn owns(&self, ledger_name: &str) -> bool {
        std::iter::once(&self.id)
            .chain(&self.aliases)
            .any(|name| name.eq_ignore_ascii_case(ledger_name))
    }
}

/// The hosts of the registry text that must report: `local` and `ssh` hosts. A `keeper`-transport host has
/// no install path (`host-sync --install --fleet` names it a failure on its own), so it has no ledger to ask for.
pub fn expected_hosts(registry: &str) -> Result<Vec<ExpectedHost>, String> {
    let value: serde_json::Value =
        serde_json::from_str(registry).map_err(|e| format!("hosts registry: {e}"))?;
    let hosts = value["hosts"]
        .as_array()
        .ok_or("hosts registry has no hosts list")?;
    let names = |v: &serde_json::Value| -> Vec<String> {
        v.as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|n| n.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default()
    };
    Ok(hosts
        .iter()
        .filter(|h| matches!(h["transport"].as_str(), Some("local" | "ssh")))
        .filter_map(|h| {
            Some(ExpectedHost {
                id: h["id"].as_str()?.to_owned(),
                aliases: names(&h["aliases"]),
            })
        })
        .collect())
}

/// Every refusal of the deploy part of the GO check at `now`: every expected host has a fresh ledger
/// (the newest file under its id or an alias), and no entry of those ledgers is over the limit.
pub fn check(
    ledgers: &[(String, Ledger)],
    expected: &[ExpectedHost],
    pins: &Pins,
    exceptions: &[Exception],
    now: i64,
) -> Vec<Finding> {
    if expected.is_empty() {
        return vec![Finding {
            code: "deploy_hosts_unknown",
            detail: "no expected host: the hosts registry is missing, unreadable or lists no local or ssh host".into(),
        }];
    }
    let mut out = Vec::new();
    for host in expected {
        let Some(ledger) = ledger_for(ledgers, host) else {
            out.push(Finding {
                code: "deploy_ledger_missing",
                detail: format!(
                    "{}: no deploy-status ledger; run `plangraph host-sync --install --check --fleet` from the center",
                    host.id
                ),
            });
            continue;
        };
        let host = &host.id;
        let short = |v: &Option<String>| {
            v.as_deref()
                .map_or("none".to_owned(), |t| t[..t.len().min(12)].to_owned())
        };
        if ledger.manifest.as_deref() != Some(pins.manifest.as_str())
            || ledger.stamp.as_deref() != Some(pins.stamp.as_str())
        {
            out.push(Finding {
                code: "deploy_ledger_outdated",
                detail: format!(
                    "{host}: report is for manifest {} and release {}, this checkout has {} and {}; run `plangraph host-sync --install --fleet` from the center",
                    short(&ledger.manifest),
                    short(&ledger.stamp),
                    &pins.manifest[..pins.manifest.len().min(12)],
                    &pins.stamp[..pins.stamp.len().min(12)]
                ),
            });
            continue;
        }
        let stale = ledger::age_hours(ledger.written_at, now);
        if now - ledger.written_at > LEDGER_MAX_AGE_HOURS * 3600 {
            out.push(Finding {
                code: "deploy_ledger_stale",
                detail: format!(
                    "{host}: deploy-status last ran {stale}h ago (limit {LEDGER_MAX_AGE_HOURS}h)"
                ),
            });
        }
        for (key, since) in &ledger.not_live_since {
            // Seconds, not rounded hours: 24 h and one second is over the limit.
            if now - since <= LIMIT_HOURS * 3600 {
                continue;
            }
            let age = ledger::age_hours(*since, now);
            let excused = exceptions
                .iter()
                .any(|e| e.entry == *key && until_epoch(&e.until).is_some_and(|end| now < end));
            if !excused {
                let state = ledger.state.get(key).map_or("not live", String::as_str);
                out.push(Finding {
                    code: "deploy_not_live",
                    detail: format!("{host}: {key} is {state} for {age}h (limit {LIMIT_HOURS}h); install it or add a dated routing exception"),
                });
            }
        }
    }
    out
}
