use super::adapters::{quota_snapshots, Env};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
fn field<'a>(r: &'a Value, names: &[&str]) -> Option<&'a Value> {
    names.iter().find_map(|n| r.get(n).filter(|v| !v.is_null()))
}
fn identity(row: &Value) -> Option<(&str, &str)> {
    let name = row["provider"].as_str()?;
    let (provider, suffix) = name.split_once(':').unwrap_or((name, ""));
    let account = field(row, &["connection_id", "connectionId"])
        .and_then(Value::as_str)
        .unwrap_or(suffix);
    Some((provider, account))
}
fn observed(row: &Value) -> Option<&Value> {
    field(row, &["created_at", "createdAt", "timestamp"])
}
fn window(row: &Value) -> Option<&str> {
    field(row, &["window_key", "windowKey"]).and_then(Value::as_str)
}
fn remaining_percent(row: &Value) -> Option<f64> {
    if field(row, &["is_exhausted", "isExhausted"])
        .is_some_and(|v| v == true || v == 1 || v == "1" || v == "true")
    {
        return Some(0.0);
    }
    field(
        row,
        &[
            "remaining_percentage",
            "remainingPercentage",
            "remainingPct",
        ],
    )
    .and_then(Value::as_f64)
    .filter(|q| (0.0..=100.0).contains(q))
}
fn stamp(v: &Value) -> Option<i64> {
    if let Some(n) = v.as_f64() {
        return n.is_finite().then_some(if n > 1e11 {
            (n / 1000.0) as i64
        } else {
            n as i64
        });
    }
    let s = v.as_str()?;
    chrono::DateTime::parse_from_rfc3339(s)
        .map(|t| t.timestamp())
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S%.f")
                .ok()
                .map(|t| t.and_utc().timestamp())
        })
}
/// Latest snapshot per account/window; stale or malformed windows disable that
/// account, never an independent healthy account. Hourly store default: 3 hours.
pub(super) fn remaining(rows: &Value, now: i64, max_age: i64) -> Option<f64> {
    remaining_for(rows, "codex", now, max_age)
}

pub(crate) fn remaining_for(rows: &Value, target: &str, now: i64, max_age: i64) -> Option<f64> {
    let mut latest: BTreeMap<(String, String), (i64, Option<f64>)> = BTreeMap::new();
    let mut invalid = BTreeSet::new();
    for r in rows.as_array()? {
        let Some((provider, account)) = identity(r) else {
            continue;
        };
        if provider != target {
            continue;
        }
        let Some(window) = window(r) else {
            invalid.insert(account.to_owned());
            continue;
        };
        let Some(at) = observed(r).and_then(stamp) else {
            invalid.insert(account.to_owned());
            continue;
        };
        let q = if at > now || now.saturating_sub(at) > max_age {
            None
        } else {
            remaining_percent(r)
        };
        let value = latest
            .entry((account.into(), window.into()))
            .or_insert((at, q));
        if at >= value.0 {
            *value = (at, q);
        }
    }
    let mut accounts: BTreeMap<String, Option<f64>> = BTreeMap::new();
    for ((a, _), (_, q)) in latest {
        accounts
            .entry(a)
            .and_modify(|v| *v = v.zip(q).map(|(a, b)| a.min(b)))
            .or_insert(q);
    }
    accounts
        .into_iter()
        .filter(|(a, _)| !invalid.contains(a))
        .filter_map(|(_, q)| q)
        .reduce(f64::max)
}

/// Whether the rows hold any observation of the codex provider (any account).
fn has_codex_rows(rows: &Value) -> bool {
    rows.as_array().is_some_and(|rows| {
        rows.iter()
            .any(|r| identity(r).is_some_and(|(provider, _)| provider == "codex"))
    })
}

/// Parse the center's quota snapshot file (`{"observed_at":<unix s>,"rows":[..]}`, the rows in
/// the reader's own format) and accept it only while it is at most `max_age` seconds old.
/// A missing, unreadable or stale file refuses as `quota_stale` and names its age.
fn center_rows(
    text: Option<&str>,
    now: i64,
    max_age: i64,
    observation_max_age: i64,
) -> Result<Value, String> {
    let doc: Value = text
        .and_then(|t| serde_json::from_str(t).ok())
        .ok_or("quota_stale: no readable center quota snapshot (age unknown)")?;
    let at = doc["observed_at"]
        .as_i64()
        .ok_or("quota_stale: center quota snapshot has no observed_at (age unknown)")?;
    let age = now.saturating_sub(at);
    if at > now || age > max_age {
        return Err(format!(
            "quota_stale: center quota snapshot is {age}s old (limit {max_age}s)"
        ));
    }
    let rows = doc
        .get("rows")
        .filter(|rows| rows.is_array())
        .cloned()
        .ok_or("quota_stale: center quota snapshot has no rows (age unknown)")?;
    // A fresh envelope around old or empty observations is still stale quota: say how old.
    if remaining(&rows, now, observation_max_age).is_none() {
        let newest = newest_codex_age(&rows, now).map_or_else(
            || "no codex observation".to_string(),
            |a| format!("newest {a}s old"),
        );
        return Err(format!(
            "quota_stale: center quota snapshot is {age}s old but holds no usable codex observation ({newest}, limit {observation_max_age}s)"
        ));
    }
    Ok(rows)
}

/// Age in seconds of the newest codex observation in `rows`, if any carries a timestamp.
fn newest_codex_age(rows: &Value, now: i64) -> Option<i64> {
    rows.as_array()?
        .iter()
        .filter(|r| identity(r).is_some_and(|(provider, _)| provider == "codex"))
        .filter_map(|r| observed(r).and_then(stamp))
        .max()
        .map(|at| now.saturating_sub(at))
}

/// The observation-age limit the feeder reads with (`quota_max_age_seconds`), taken from the
/// same policy text, so what the center publishes is exactly what a worker would accept.
fn observation_limit(policy_text: &str) -> Result<i64, String> {
    toml::from_str::<super::Policy>(policy_text)
        .map(|p| p.quota_max_age_seconds)
        .map_err(|_| "invalid policy".to_string())
}

/// Pull `--policy PATH` out of the arguments; the rest are the verb's own.
fn split_policy_arg(args: &[String]) -> Result<(Vec<String>, Option<String>), String> {
    let mut rest = Vec::new();
    let mut policy = None;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        if arg == "--policy" {
            policy = Some(
                it.next()
                    .filter(|p| !p.is_empty())
                    .ok_or("--policy requires a path")?
                    .clone(),
            );
        } else {
            rest.push(arg.clone());
        }
    }
    Ok((rest, policy))
}

/// The limit for publishing, from `--policy` or the feeder's default `deploy/feeder/policy.toml`.
fn publish_limit(policy: Option<&str>) -> Result<i64, String> {
    let path = policy.unwrap_or("deploy/feeder/policy.toml");
    let text = std::fs::read_to_string(path).map_err(|_| "policy unreadable".to_string())?;
    observation_limit(&text)
}

/// Publishing needs a usable codex observation; otherwise the previous file stays and ages, so
/// workers report `quota_stale` with its age instead of a fresh envelope around nothing.
fn publishable(rows: &Value, now: i64, max_age: i64) -> Result<(), String> {
    if remaining(rows, now, max_age).is_some() {
        return Ok(());
    }
    let newest = newest_codex_age(rows, now).map_or_else(
        || "no codex observation".to_string(),
        |a| format!("newest {a}s old"),
    );
    Err(format!("no usable codex observation to publish ({newest})"))
}

/// The quota rows admission reads. A host with its own codex observations (the center) uses
/// them. A host with none, and no quota supplied with its facts, reads the center's snapshot
/// file instead, so quota is never silently null there.
pub(super) fn rows_for_host(
    local: Option<Value>,
    supplied: Option<f64>,
    center: &Path,
    now: i64,
    max_age: i64,
    observation_max_age: i64,
) -> Result<Option<Value>, String> {
    if supplied.is_some() || local.as_ref().is_some_and(has_codex_rows) {
        return Ok(local);
    }
    center_rows(
        std::fs::read_to_string(center).ok().as_deref(),
        now,
        max_age,
        observation_max_age,
    )
    .map(Some)
}

/// Write the quota rows the center reads, for hosts without a quota reader; atomic replace.
fn export(rows: &Value, path: &Path, now: i64) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, json!({"observed_at":now,"rows":rows}).to_string())?;
    std::fs::rename(tmp, path)
}

fn valid_destination(destination: &str) -> Result<(), String> {
    if destination.is_empty()
        || destination.starts_with('-')
        || destination
            .chars()
            .any(|c| c.is_whitespace() || c.is_control())
    {
        return Err("quota push: invalid SSH destination".into());
    }
    Ok(())
}

/// The policy a worker's feeder reads, relative to its home: `pg-feeder.service` runs in
/// `~/code/plangraph` with the default `deploy/feeder/policy.toml`.
const WORKER_POLICY: &str = "code/plangraph/deploy/feeder/policy.toml";

/// The observation-age limit the worker at `destination` reads with, from its own policy file
/// over SSH. An unreadable or invalid worker policy refuses publishing rather than guessing.
fn worker_limit(destination: &str, ssh: &str) -> Result<i64, String> {
    valid_destination(destination)?;
    let mut cmd = std::process::Command::new(ssh);
    cmd.args([
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        destination,
        &format!("cat {WORKER_POLICY}"),
    ]);
    let text = pg_worker_core::host_command::output(&mut cmd, std::time::Duration::from_secs(15))
        .map_err(|e| format!("quota push: cannot read the worker policy: {e}"))?;
    observation_limit(&String::from_utf8_lossy(&text))
        .map_err(|e| format!("quota push: worker policy: {e}"))
}

/// Publish `rows` to the worker only if they hold a codex observation the worker will accept,
/// judged by the worker's own `quota_max_age_seconds`.
fn push_snapshot(rows: &Value, destination: &str, ssh: &str, now: i64) -> Result<(), String> {
    let limit = worker_limit(destination, ssh)?;
    publishable(rows, now, limit).map_err(|why| format!("quota push: {why}"))?;
    let doc = json!({"observed_at":now,"rows":rows});
    push(&doc.to_string(), destination, ssh)
}

/// Install `doc` as the snapshot file of the host at SSH `destination` (the destination the
/// host-link units already use), by atomic replace on that host. `ssh` is the client program.
fn push(doc: &str, destination: &str, ssh: &str) -> Result<(), String> {
    use std::io::Write;
    valid_destination(destination)?;
    let remote = "umask 077 && mkdir -p ~/.config/plangraph && cat > ~/.config/plangraph/quota-facts.json.tmp && mv ~/.config/plangraph/quota-facts.json.tmp ~/.config/plangraph/quota-facts.json";
    let mut child = std::process::Command::new(ssh)
        .args([
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            destination,
            remote,
        ])
        .stdin(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| format!("quota push: cannot start {ssh}: {e}"))?;
    child
        .stdin
        .take()
        .ok_or("quota push: no stdin")?
        .write_all(doc.as_bytes())
        .map_err(|e| format!("quota push: write failed: {e}"))?;
    let status = child.wait().map_err(|e| format!("quota push: {e}"))?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| format!("quota push: {ssh} exited with {status}"))
}

/// Print the most recent available observation for each provider, account and window.
pub fn run_status(env: &Env, args: &[String]) -> i32 {
    let mut effective = env.clone();
    let (args, policy) = match split_policy_arg(args) {
        Ok(split) => split,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let exporting = args.iter().any(|a| a == "--export");
    let limit = if exporting {
        match publish_limit(policy.as_deref()) {
            Ok(limit) => limit,
            Err(e) => {
                eprintln!("quota publish: {e}");
                return 2;
            }
        }
    } else {
        0
    };
    match args.as_slice() {
        [] => {}
        [flag, path] if flag == "--db" && !path.is_empty() => {
            effective.insert("QUOTA_SNAPSHOT_DB".into(), path.clone());
        }
        [flag, path] if flag == "--export" && !path.is_empty() => {
            let rows = std::env::current_dir()
                .ok()
                .and_then(|repo| quota_snapshots(&effective, &repo).ok())
                .filter(|rows| rows.is_array());
            let Some(rows) = rows else {
                eprintln!("quota export: reader unavailable");
                return 2;
            };
            let now = chrono::Utc::now().timestamp();
            if let Err(why) = publishable(&rows, now, limit) {
                eprintln!("quota export: {why}");
                return 2;
            }
            if export(&rows, Path::new(path), now).is_err() {
                eprintln!("quota export: cannot write the snapshot file");
                return 2;
            }
            return 0;
        }
        [flag, destination] if flag == "--push" => {
            let rows = std::env::current_dir()
                .ok()
                .and_then(|repo| quota_snapshots(&effective, &repo).ok())
                .filter(|rows| rows.is_array());
            let Some(rows) = rows else {
                eprintln!("quota push: reader unavailable");
                return 2;
            };
            let now = chrono::Utc::now().timestamp();
            return match push_snapshot(&rows, destination, "ssh", now) {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("{e}");
                    2
                }
            };
        }
        _ => {
            eprintln!(
                "usage: plangraph quota [--db PATH | --export PATH [--policy PATH] | --push SSH_DESTINATION]"
            );
            return 2;
        }
    }
    let result = std::env::current_dir()
        .ok()
        .and_then(|repo| quota_snapshots(&effective, &repo).ok());
    let mut latest = BTreeMap::new();
    for row in result
        .as_ref()
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let Some((provider, account)) = identity(row) else {
            continue;
        };
        let Some(window) = window(row) else { continue };
        let Some(observed) = observed(row) else {
            continue;
        };
        let Some(at) = stamp(observed) else { continue };
        if [provider, account, window]
            .iter()
            .any(|s| s.chars().any(char::is_control))
        {
            continue;
        }
        let entry = latest
            .entry((provider, account, window))
            .or_insert((at, row, observed));
        if at >= entry.0 {
            *entry = (at, row, observed);
        }
    }
    if latest.is_empty() {
        println!("quota unknown");
    } else {
        println!("provider\taccount\twindow\tused / limit\tobserved at");
        for ((provider, account, window), (_, row, observed)) in latest {
            let usage = remaining_percent(row).map_or_else(
                || "unknown".into(),
                |left| {
                    let used = format!("{:.1}", 100.0 - left);
                    format!("{}% / 100%", used.trim_end_matches(".0"))
                },
            );
            println!("{provider}\t{account}\t{window}\t{usage}\t{observed}");
        }
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn doc(at: i64) -> String {
        json!({"observed_at":at,"rows":[{"provider":"codex:a","windowKey":"5h","timestamp":at,"remainingPct":70}]}).to_string()
    }
    #[test]
    fn center_snapshot_is_used_while_fresh_and_names_its_age_when_stale() {
        let now = 1_800_000_000;
        let rows = center_rows(Some(&doc(now - 600)), now, 900, 10_800).unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        let stale = center_rows(Some(&doc(now - 5_400)), now, 900, 10_800).unwrap_err();
        assert_eq!(
            stale,
            "quota_stale: center quota snapshot is 5400s old (limit 900s)"
        );
        for bad in [
            None,
            Some("not json"),
            Some("{\"rows\":[]}"),
            Some("{\"observed_at\":1}"),
        ] {
            assert!(center_rows(bad, now, 900, 10_800)
                .unwrap_err()
                .starts_with("quota_stale:"));
        }
        assert!(center_rows(Some(&doc(now + 5)), now, 900, 10_800).is_err());
    }
    #[test]
    fn a_fresh_envelope_around_old_or_empty_observations_is_stale_quota_with_ages() {
        let now = 1_800_000_000;
        let wrap = |rows: Value| json!({"observed_at":now - 30,"rows":rows}).to_string();
        let old = json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 20_000,"remainingPct":70}]);
        let err = center_rows(Some(&wrap(old.clone())), now, 900, 10_800).unwrap_err();
        assert_eq!(
            err,
            "quota_stale: center quota snapshot is 30s old but holds no usable codex observation (newest 20000s old, limit 10800s)"
        );
        let err = center_rows(Some(&wrap(json!([]))), now, 900, 10_800).unwrap_err();
        assert!(err.contains("no codex observation"), "{err}");
        // The center refuses to publish either, so the worker's file keeps aging instead.
        assert!(publishable(&old, now, 10_800)
            .unwrap_err()
            .contains("newest 20000s old"));
        assert!(publishable(&json!([]), now, 10_800).is_err());
        let fresh =
            json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 60,"remainingPct":70}]);
        assert!(publishable(&fresh, now, 10_800).is_ok());
    }
    #[test]
    fn publishing_uses_the_policys_observation_age_limit_like_the_reader() {
        let now = 1_800_000_000;
        let four_hours = json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 14_400,"remainingPct":70}]);
        let default = observation_limit("").unwrap();
        assert_eq!(default, 10_800);
        assert!(publishable(&four_hours, now, default).is_err());
        // A six-hour policy publishes it, and the worker's read with the same limit accepts it.
        let six = observation_limit("quota_max_age_seconds = 21600\n").unwrap();
        assert_eq!(six, 21_600);
        assert!(publishable(&four_hours, now, six).is_ok());
        let text = json!({"observed_at":now,"rows":four_hours}).to_string();
        assert!(center_rows(Some(&text), now, 900, six).is_ok());
        assert!(center_rows(Some(&text), now, 900, default).is_err());
        assert!(observation_limit("quota_max_age_seconds = \"x\"").is_err());
    }
    #[test]
    fn the_policy_argument_is_split_from_the_verb_arguments() {
        let a = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let (rest, policy) = split_policy_arg(&a(&["--push", "w", "--policy", "p.toml"])).unwrap();
        assert_eq!((rest, policy), (a(&["--push", "w"]), Some("p.toml".into())));
        assert_eq!(split_policy_arg(&a(&["--db", "x"])).unwrap().1, None);
        assert!(split_policy_arg(&a(&["--policy"])).is_err());
        assert!(publish_limit(Some("/nonexistent/policy.toml")).is_err());
    }
    #[test]
    fn a_host_without_codex_rows_reads_the_center_file_and_never_gets_null_quota() {
        let now = 1_800_000_000;
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("quota-facts.json");
        let local = Some(json!([]));
        let err = rows_for_host(local.clone(), None, &file, now, 900, 10_800).unwrap_err();
        assert!(err.starts_with("quota_stale:"), "{err}");
        std::fs::write(&file, doc(now - 60)).unwrap();
        let rows = rows_for_host(local.clone(), None, &file, now, 900, 10_800)
            .unwrap()
            .unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        // Own codex rows (the center) and supplied facts quota both skip the file.
        let own = Some(
            json!([{"provider":"codex:b","windowKey":"5h","timestamp":now,"remainingPct":40}]),
        );
        std::fs::remove_file(&file).unwrap();
        assert_eq!(
            rows_for_host(own.clone(), None, &file, now, 900, 10_800).unwrap(),
            own
        );
        assert_eq!(
            rows_for_host(None, Some(50.0), &file, now, 900, 10_800).unwrap(),
            None
        );
    }
    /// A stand-in ssh: serves `policy` for the worker policy read and keeps what a push sends.
    #[cfg(unix)]
    fn fake_ssh(dir: &Path, policy: Option<&str>) -> (std::path::PathBuf, std::path::PathBuf) {
        use std::os::unix::fs::PermissionsExt;
        let (ssh, out) = (dir.join("ssh"), dir.join("received"));
        let serve = match policy {
            Some(p) => {
                std::fs::write(dir.join("policy.toml"), p).unwrap();
                format!("cat {}", dir.join("policy.toml").display())
            }
            None => "exit 1".to_string(),
        };
        let script = format!(
            "#!/bin/sh\nfor last; do :; done\ncase \"$last\" in\n  cat\\ *) {serve} ;;\n  *) cat > {} ;;\nesac\n",
            out.display()
        );
        std::fs::write(&ssh, script).unwrap();
        std::fs::set_permissions(&ssh, std::fs::Permissions::from_mode(0o755)).unwrap();
        (ssh, out)
    }
    #[cfg(unix)]
    #[test]
    fn the_deployed_push_judges_observations_by_the_workers_own_age_limit() {
        let now = 1_800_000_000;
        let four_hours = json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 14_400,"remainingPct":70}]);
        // A worker on a six-hour policy accepts a four-hour observation: it is published, and
        // the file the worker then reads is accepted with that same limit.
        let d = tempfile::tempdir().unwrap();
        let (ssh, out) = fake_ssh(d.path(), Some("quota_max_age_seconds = 21600\n"));
        push_snapshot(&four_hours, "worker.example", ssh.to_str().unwrap(), now).unwrap();
        let sent = std::fs::read_to_string(&out).unwrap();
        assert!(center_rows(Some(&sent), now + 30, 900, 21_600).is_ok());
        // A worker on the default policy refuses it: nothing is sent.
        let d = tempfile::tempdir().unwrap();
        let (ssh, out) = fake_ssh(d.path(), Some("quota_floor_percent = 10.0\n"));
        let err =
            push_snapshot(&four_hours, "worker.example", ssh.to_str().unwrap(), now).unwrap_err();
        assert!(err.contains("no usable codex observation"), "{err}");
        assert!(!out.exists());
        // An unreadable worker policy refuses rather than guessing.
        let d = tempfile::tempdir().unwrap();
        let (ssh, out) = fake_ssh(d.path(), None);
        let err =
            push_snapshot(&four_hours, "worker.example", ssh.to_str().unwrap(), now).unwrap_err();
        assert!(err.contains("worker policy"), "{err}");
        assert!(!out.exists());
        assert!(worker_limit("-oProxyCommand=x", "ssh").is_err());
    }
    #[test]
    fn the_worker_policy_path_is_the_one_the_feeder_service_reads() {
        let unit = include_str!("../../../../deploy/systemd/pg-feeder.service");
        let dir = unit
            .lines()
            .find_map(|l| l.strip_prefix("WorkingDirectory=%h/"))
            .expect("working directory");
        assert_eq!(WORKER_POLICY, format!("{dir}/deploy/feeder/policy.toml"));
    }
    #[cfg(unix)]
    #[test]
    fn push_streams_the_snapshot_to_the_remote_command_and_refuses_unsafe_destinations() {
        use std::os::unix::fs::PermissionsExt;
        let d = tempfile::tempdir().unwrap();
        let out = d.path().join("received");
        let args = d.path().join("args");
        // A stand-in ssh: records its arguments and keeps what arrives on stdin.
        let ssh = d.path().join("ssh");
        std::fs::write(
            &ssh,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > {}\ncat > {}\n",
                args.display(),
                out.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&ssh, std::fs::Permissions::from_mode(0o755)).unwrap();
        let doc = json!({"observed_at":1,"rows":[]}).to_string();
        push(&doc, "worker.example", ssh.to_str().unwrap()).unwrap();
        assert_eq!(std::fs::read_to_string(&out).unwrap(), doc);
        let recorded = std::fs::read_to_string(&args).unwrap();
        assert!(recorded.contains("BatchMode=yes") && recorded.contains("worker.example"));
        assert!(recorded.contains("quota-facts.json.tmp && mv"));
        for bad in ["", "-oProxyCommand=x", "a b", "a\nb"] {
            assert!(push(&doc, bad, ssh.to_str().unwrap()).is_err(), "{bad:?}");
        }
        assert!(push(&doc, "worker.example", "/nonexistent/ssh").is_err());
    }
    #[test]
    fn export_writes_the_snapshot_the_center_file_reader_accepts() {
        let now = 1_800_000_000;
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("quota-facts.json");
        let rows =
            json!([{"provider":"codex:a","windowKey":"5h","timestamp":now,"remainingPct":55}]);
        export(&rows, &file, now).unwrap();
        let text = std::fs::read_to_string(&file).unwrap();
        let back = center_rows(Some(&text), now + 30, 900, 10_800).unwrap();
        assert_eq!(remaining(&back, now + 30, 10_800), Some(55.0));
    }
    #[test]
    fn aliases_exhaustion_freshness_and_account_isolation() {
        let now = 1_800_000_000;
        let row = |a: &str, w: &str, at: i64, q: Value| json!({"provider":format!("codex:{a}"),"windowKey":w,"timestamp":at,"remainingPct":q});
        let mut rows = vec![
            row("a", "day", now - 10, json!(90)),
            row("a", "week", now - 10, json!(30)),
            row("b", "day", now - 20, json!(99)),
            row("b", "day", now - 5, json!(2)),
            row("broken", "day", now - 5, json!("bad")),
        ];
        assert_eq!(remaining(&json!(rows), now, 10800), Some(30.0));
        rows[0]["isExhausted"] = json!("true");
        assert_eq!(remaining(&json!(rows), now, 10800), Some(2.0));
        rows[3]["isExhausted"] = json!("1");
        assert_eq!(remaining(&json!(rows), now, 10800), Some(0.0));
        assert_eq!(
            remaining(
                &json!([row("a", "day", now - 10801, json!(99))]),
                now,
                10800
            ),
            None
        );
        assert_eq!(
            remaining(&json!([row("a", "day", now + 1, json!(99))]), now, 10800),
            None
        );
        assert_eq!(remaining(&json!([]), now, 10800), None);
    }
    #[test]
    fn newest_malformed_window_cannot_reuse_older_high_quota() {
        let rows = json!([
            {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:00Z","remaining_percentage":99},
            {"provider":"codex","connection_id":"a","window_key":"day","created_at":"2026-01-01T00:00:01Z","remaining_percentage":null},
            {"provider":"codex","connection_id":"b","window_key":"day","created_at":"2026-01-01 00:00:01","remaining_percentage":40}
        ]);
        assert_eq!(remaining(&rows, 1767225602, 10800), Some(40.0));
    }
}
