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
fn center_rows(text: Option<&str>, now: i64, max_age: i64) -> Result<Value, String> {
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
    doc.get("rows")
        .filter(|rows| rows.is_array())
        .cloned()
        .ok_or_else(|| "quota_stale: center quota snapshot has no rows (age unknown)".into())
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
) -> Result<Option<Value>, String> {
    if supplied.is_some() || local.as_ref().is_some_and(has_codex_rows) {
        return Ok(local);
    }
    center_rows(
        std::fs::read_to_string(center).ok().as_deref(),
        now,
        max_age,
    )
    .map(Some)
}

/// Write the quota rows the center reads, for hosts without a quota reader; atomic replace.
fn export(rows: &Value, path: &Path, now: i64) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    std::fs::write(&tmp, json!({"observed_at":now,"rows":rows}).to_string())?;
    std::fs::rename(tmp, path)
}

/// Install `doc` as the snapshot file of the host at SSH `destination` (the destination the
/// host-link units already use), by atomic replace on that host. `ssh` is the client program.
fn push(doc: &str, destination: &str, ssh: &str) -> Result<(), String> {
    use std::io::Write;
    if destination.is_empty()
        || destination.starts_with('-')
        || destination
            .chars()
            .any(|c| c.is_whitespace() || c.is_control())
    {
        return Err("quota push: invalid SSH destination".into());
    }
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
    match args {
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
            if export(&rows, Path::new(path), chrono::Utc::now().timestamp()).is_err() {
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
            let doc = json!({"observed_at":chrono::Utc::now().timestamp(),"rows":rows});
            return match push(&doc.to_string(), destination, "ssh") {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("{e}");
                    2
                }
            };
        }
        _ => {
            eprintln!(
                "usage: plangraph quota [--db PATH | --export PATH | --push SSH_DESTINATION]"
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
        let rows = center_rows(Some(&doc(now - 600)), now, 900).unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        let stale = center_rows(Some(&doc(now - 5_400)), now, 900).unwrap_err();
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
            assert!(center_rows(bad, now, 900)
                .unwrap_err()
                .starts_with("quota_stale:"));
        }
        assert!(center_rows(Some(&doc(now + 5)), now, 900).is_err());
    }
    #[test]
    fn a_host_without_codex_rows_reads_the_center_file_and_never_gets_null_quota() {
        let now = 1_800_000_000;
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("quota-facts.json");
        let local = Some(json!([]));
        let err = rows_for_host(local.clone(), None, &file, now, 900).unwrap_err();
        assert!(err.starts_with("quota_stale:"), "{err}");
        std::fs::write(&file, doc(now - 60)).unwrap();
        let rows = rows_for_host(local.clone(), None, &file, now, 900)
            .unwrap()
            .unwrap();
        assert_eq!(remaining(&rows, now, 10_800), Some(70.0));
        // Own codex rows (the center) and supplied facts quota both skip the file.
        let own = Some(
            json!([{"provider":"codex:b","windowKey":"5h","timestamp":now,"remainingPct":40}]),
        );
        std::fs::remove_file(&file).unwrap();
        assert_eq!(
            rows_for_host(own.clone(), None, &file, now, 900).unwrap(),
            own
        );
        assert_eq!(
            rows_for_host(None, Some(50.0), &file, now, 900).unwrap(),
            None
        );
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
        let back = center_rows(Some(&text), now + 30, 900).unwrap();
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
