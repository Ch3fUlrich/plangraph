//! Publishing the quota snapshot: the center exports it to a file or pushes it to a worker over
//! SSH, only while it holds a codex observation the reading worker will accept.
use super::quota::{newest_codex_age, remaining};
use serde_json::{json, Value};
use std::path::Path;

/// The observation-age limit the feeder reads with (`quota_max_age_seconds`), taken from the
/// same policy text, so what the center publishes is exactly what a worker would accept.
fn observation_limit(policy_text: &str) -> Result<i64, String> {
    toml::from_str::<super::Policy>(policy_text)
        .map(|p| p.quota_max_age_seconds)
        .map_err(|_| "invalid policy".to_string())
}

/// The options `quota` takes beside its verb: the feeder policy of `--export` (`--policy`) and
/// the policy file the worker's feeder reads, relative to its home (`--worker-policy`).
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct Options {
    pub policy: Option<String>,
    pub worker_policy: Option<String>,
}

/// Pull the two policy options out of the arguments; the rest are the verb's own.
pub(super) fn split_options(args: &[String]) -> Result<(Vec<String>, Options), String> {
    let mut rest = Vec::new();
    let mut options = Options::default();
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let slot = match arg.as_str() {
            "--policy" => &mut options.policy,
            "--worker-policy" => &mut options.worker_policy,
            _ => {
                rest.push(arg.clone());
                continue;
            }
        };
        *slot = Some(
            it.next()
                .filter(|p| !p.is_empty())
                .ok_or_else(|| format!("{arg} requires a path"))?
                .clone(),
        );
    }
    Ok((rest, options))
}

/// A worker policy path is relative to the worker's home and made of plain path characters, so
/// it cannot carry shell syntax into the remote `cat` or climb out of the home directory.
fn valid_worker_policy(path: &str) -> Result<(), String> {
    let plain = path
        .bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"._-/".contains(&b));
    if path.is_empty()
        || path.len() > 200
        || path.starts_with(['/', '-'])
        || !plain
        || path.split('/').any(|c| c == ".." || c.is_empty())
    {
        return Err("quota push: invalid worker policy path".into());
    }
    Ok(())
}

/// The limit for publishing, from `--policy` or the feeder's default `deploy/feeder/policy.toml`.
pub(super) fn publish_limit(policy: Option<&str>) -> Result<i64, String> {
    let path = policy.unwrap_or("deploy/feeder/policy.toml");
    let text = std::fs::read_to_string(path).map_err(|_| "policy unreadable".to_string())?;
    observation_limit(&text)
}

/// Publishing needs a usable codex observation; otherwise the previous file stays and ages, so
/// workers report `quota_stale` with its age instead of a fresh envelope around nothing.
pub(super) fn publishable(rows: &Value, now: i64, max_age: i64) -> Result<(), String> {
    if remaining(rows, now, max_age).is_some() {
        return Ok(());
    }
    let newest = newest_codex_age(rows, now).map_or_else(
        || "no codex observation".to_string(),
        |a| format!("newest {a}s old"),
    );
    Err(format!("no usable codex observation to publish ({newest})"))
}

/// Write the quota rows the center reads, for hosts without a quota reader; atomic replace.
pub(super) fn export(rows: &Value, path: &Path, now: i64) -> std::io::Result<()> {
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

/// The observation-age limit the worker at `destination` reads with, from the policy file its
/// feeder uses (`policy`, relative to its home; default `WORKER_POLICY`) over SSH. An unreadable
/// or invalid worker policy refuses publishing rather than guessing.
fn worker_limit(destination: &str, ssh: &str, policy: Option<&str>) -> Result<i64, String> {
    valid_destination(destination)?;
    let policy = policy.unwrap_or(WORKER_POLICY);
    valid_worker_policy(policy)?;
    let mut cmd = std::process::Command::new(ssh);
    cmd.args([
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        destination,
        &format!("cat {policy}"),
    ]);
    let text = pg_worker_core::host_command::output(&mut cmd, std::time::Duration::from_secs(15))
        .map_err(|e| format!("quota push: cannot read the worker policy: {e}"))?;
    observation_limit(&String::from_utf8_lossy(&text))
        .map_err(|e| format!("quota push: worker policy: {e}"))
}

/// Publish `rows` to the worker only if they hold a codex observation the worker will accept,
/// judged by the worker's own `quota_max_age_seconds`.
pub(super) fn push_snapshot(
    rows: &Value,
    destination: &str,
    ssh: &str,
    worker_policy: Option<&str>,
    now: i64,
) -> Result<(), String> {
    let limit = worker_limit(destination, ssh, worker_policy)?;
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

#[cfg(test)]
mod tests {
    use super::super::quota::center_rows;
    use super::*;
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
    fn the_policy_arguments_are_split_from_the_verb_arguments() {
        let a = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        let (rest, o) = split_options(&a(&[
            "--push",
            "w",
            "--policy",
            "p.toml",
            "--worker-policy",
            "etc/feeder.toml",
        ]))
        .unwrap();
        assert_eq!(rest, a(&["--push", "w"]));
        assert_eq!(
            (o.policy.as_deref(), o.worker_policy.as_deref()),
            (Some("p.toml"), Some("etc/feeder.toml"))
        );
        assert_eq!(
            split_options(&a(&["--db", "x"])).unwrap().1,
            Options::default()
        );
        assert!(split_options(&a(&["--policy"])).is_err());
        assert!(split_options(&a(&["--worker-policy", ""])).is_err());
        assert!(publish_limit(Some("/nonexistent/policy.toml")).is_err());
    }
    #[test]
    fn a_worker_policy_path_stays_inside_the_workers_home_and_is_plain() {
        for ok in [
            "code/plangraph/deploy/feeder/policy.toml",
            "etc/feeder-2.toml",
        ] {
            assert!(valid_worker_policy(ok).is_ok(), "{ok}");
        }
        for bad in [
            "",
            "/etc/feeder.toml",
            "-oProxyCommand=x",
            "a/../b",
            "a//b",
            "a b",
            "a;rm",
            "$(x)",
            "a\nb",
        ] {
            assert!(valid_worker_policy(bad).is_err(), "{bad:?}");
        }
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
        push_snapshot(
            &four_hours,
            "worker.example",
            ssh.to_str().unwrap(),
            None,
            now,
        )
        .unwrap();
        let sent = std::fs::read_to_string(&out).unwrap();
        assert!(center_rows(Some(&sent), now + 30, 900, 21_600).is_ok());
        // A worker on the default policy refuses it: nothing is sent.
        let d = tempfile::tempdir().unwrap();
        let (ssh, out) = fake_ssh(d.path(), Some("quota_floor_percent = 10.0\n"));
        let err = push_snapshot(
            &four_hours,
            "worker.example",
            ssh.to_str().unwrap(),
            None,
            now,
        )
        .unwrap_err();
        assert!(err.contains("no usable codex observation"), "{err}");
        assert!(!out.exists());
        // An unreadable worker policy refuses rather than guessing.
        let d = tempfile::tempdir().unwrap();
        let (ssh, out) = fake_ssh(d.path(), None);
        let err = push_snapshot(
            &four_hours,
            "worker.example",
            ssh.to_str().unwrap(),
            None,
            now,
        )
        .unwrap_err();
        assert!(err.contains("worker policy"), "{err}");
        assert!(!out.exists());
        assert!(worker_limit("-oProxyCommand=x", "ssh", None).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn a_feeder_started_with_its_own_policy_path_sets_the_publishers_limit() {
        let now = 1_800_000_000;
        let four_hours = json!([{"provider":"codex:a","windowKey":"5h","timestamp":now - 14_400,"remainingPct":70}]);
        let d = tempfile::tempdir().unwrap();
        // The worker's default policy file allows three hours; its feeder runs with another
        // file (`--policy etc/feeder.toml`) that allows six. The fake serves only that path.
        let (ssh, out) = fake_ssh(d.path(), Some("quota_max_age_seconds = 21600\n"));
        let script = std::fs::read_to_string(&ssh)
            .unwrap()
            .replace("cat\\ *)", "\"cat etc/feeder.toml\")")
            // Any other policy path does not exist on this worker.
            .replace("  *) cat >", "  cat\\ *) exit 1 ;;\n  *) cat >");
        std::fs::write(&ssh, script).unwrap();
        let sshp = ssh.to_str().unwrap();
        let custom = Some("etc/feeder.toml");
        push_snapshot(&four_hours, "worker.example", sshp, custom, now).unwrap();
        assert!(out.exists());
        std::fs::remove_file(&out).unwrap();
        // Without naming the path the publisher cannot read the worker's real policy: refused.
        assert!(push_snapshot(&four_hours, "worker.example", sshp, None, now).is_err());
        assert!(!out.exists());
        assert!(push_snapshot(&four_hours, "worker.example", sshp, Some("../x"), now).is_err());
    }
    #[test]
    fn the_quota_unit_passes_the_workers_feeder_policy_to_the_push() {
        let unit = include_str!("../../../../deploy/systemd/pg-quota-facts@.service");
        assert!(unit.contains("--worker-policy ${FEEDER_POLICY}"));
        let default = unit
            .lines()
            .find_map(|l| l.strip_prefix("Environment=FEEDER_POLICY="))
            .expect("default worker policy");
        assert_eq!(default, WORKER_POLICY);
        // The per-worker file is read after the default, so it overrides it.
        let at = |needle: &str| unit.find(needle).unwrap();
        assert!(
            at("Environment=FEEDER_POLICY=")
                < at("EnvironmentFile=%h/.config/plangraph/host-links")
        );
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
}
