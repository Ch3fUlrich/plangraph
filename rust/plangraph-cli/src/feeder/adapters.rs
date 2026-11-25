use super::*;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
pub type Env = BTreeMap<String, String>;
pub(super) fn python(env: &Env, repo: &Path) -> Command {
    let mut cmd = Command::new("python3");
    cmd.current_dir(repo).envs(env);
    // Use the adapter revision from this checkout, not an unrelated editable
    // installation. Preserve additional caller module paths after that source.
    if repo.join("src/plangraph").is_dir() {
        let mut paths = vec![repo.join("src")];
        if let Some(extra) = env.get("PYTHONPATH") {
            paths.extend(std::env::split_paths(extra));
        }
        let path =
            std::env::join_paths(paths).unwrap_or_else(|_| repo.join("src").into_os_string());
        cmd.env("PYTHONPATH", path);
    }
    cmd
}
pub(super) fn output(mut cmd: Command) -> Result<Value, String> {
    let out = cmd.output().map_err(|_| "cannot execute adapter")?;
    if !out.status.success() {
        return Err("adapter returned failure".into());
    }
    serde_json::from_slice(&out.stdout).map_err(|_| "adapter returned invalid JSON".into())
}
pub(super) fn quota_snapshots(env: &Env, repo: &Path) -> Result<Value, String> {
    snapshot_rows(env, repo, "id")
}
/// The same read-only source keeping the newest observation per key (id breaks a tie).
pub(super) fn quota_snapshots_by_observation(env: &Env, repo: &Path) -> Result<Value, String> {
    snapshot_rows(env, repo, "observed")
}
fn snapshot_rows(env: &Env, repo: &Path, latest: &str) -> Result<Value, String> {
    let mut cmd = python(env, repo);
    cmd.args(["-c", "import json, sys; from plangraph.routing_support.quota_snapshots import fetch_snapshot_rows; print(json.dumps(fetch_snapshot_rows(latest=sys.argv[1])))", latest]);
    output(cmd)
}
pub trait Transport {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String>;
}
pub(super) struct Keeper<'a> {
    pub env: &'a Env,
}
/// The same keeper transport with a caller-chosen request limit, for verbs that read git.
pub struct SlowKeeper<'a> {
    pub env: &'a Env,
    pub max_time_secs: u32,
}
pub const MERGE_INDEX_EXPIRED: &str = "merge_index_expired";
const MERGE_INDEX_RUNNING: &str = "merge_index_running";

fn http_response(raw: &[u8]) -> Result<Value, String> {
    let split = raw
        .iter()
        .rposition(|b| *b == b'\n')
        .ok_or("keeper response missing status")?;
    let status = std::str::from_utf8(&raw[split + 1..])
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or("invalid HTTP status")?;
    let body: Value =
        serde_json::from_slice(&raw[..split]).map_err(|_| "keeper returned invalid JSON")?;
    if (200..300).contains(&status) {
        return Ok(body);
    }
    let code = body["error"].as_str().unwrap_or("");
    if status == 409 && matches!(code, MERGE_INDEX_EXPIRED | MERGE_INDEX_RUNNING) {
        return Err(code.to_owned());
    }
    if body["msg"]
        .as_str()
        .is_some_and(|s| s.contains("cannot read decisions"))
    {
        return Err("keeper decisions unavailable".into());
    }
    if (400..500).contains(&status)
        && matches!(
            code,
            "bad_state" | "conflict" | "epoch_stale" | "stale" | "not_ready" | "locked"
        )
    {
        Err(format!("item_refused:{code}"))
    } else {
        Err("keeper request refused or unreachable".into())
    }
}
impl Transport for Keeper<'_> {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        post(self.env, verb, body, 20)
    }
}
impl Transport for SlowKeeper<'_> {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        post(self.env, verb, body, self.max_time_secs)
    }
}
fn post(env: &Env, verb: &str, body: Option<Value>, max_time_secs: u32) -> Result<Value, String> {
    let url = env.get("PG_URL").ok_or("PG_URL missing")?;
    let path = env.get("PG_TOKEN_FILE").ok_or("PG_TOKEN_FILE missing")?;
    let token = fs::read_to_string(path).map_err(|_| "keeper token file unreadable")?;
    let token = token.trim();
    if token.is_empty()
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.=+/".contains(&b))
    {
        return Err("invalid keeper token".into());
    }
    let config = format!("header = \"Authorization: Bearer {token}\"\n");
    let mut cmd = Command::new("curl");
    cmd.args([
        "--silent",
        "--max-time",
        &max_time_secs.to_string(),
        "--proto",
        "=http,https",
        "--config",
        "-",
        "--write-out",
        "\n%{http_code}",
    ])
    .arg(format!("{}/v1/{verb}", url.trim_end_matches('/')))
    .stdin(Stdio::piped())
    .stdout(Stdio::piped())
    .stderr(Stdio::null());
    if let Some(body) = body {
        cmd.args([
            "--header",
            "Content-Type: application/json",
            "--data",
            &body.to_string(),
        ]);
    }
    let mut child = cmd.spawn().map_err(|_| "cannot execute curl")?;
    child
        .stdin
        .take()
        .ok_or("curl stdin unavailable")?
        .write_all(config.as_bytes())
        .map_err(|_| "keeper request failed")?;
    let out = child
        .wait_with_output()
        .map_err(|_| "keeper request failed")?;
    if !out.status.success() {
        return Err("keeper request refused or unreachable".into());
    }
    http_response(&out.stdout)
}
/// Parse the receipt before interpreting exit status: queued (5) and unknown (1)
/// are durable admissions. Missing/unparseable admission receipts are failures.
fn receipt(raw: &[u8], exit: Option<i32>) -> Result<Value, String> {
    let run: Value =
        serde_json::from_slice(raw).map_err(|_| "spawner failed without a JSON run receipt")?;
    if run["run_id"].as_str().is_some_and(|s| !s.is_empty())
        && matches!(
            run["status"].as_str(),
            Some("queued" | "unknown" | "planned" | "running" | "completed" | "succeeded")
        )
    {
        return Ok(run);
    }
    if matches!(
        run["status"].as_str(),
        Some(
            "failed"
                | "refused"
                | "cancelled"
                | "killed"
                | "timeout"
                | "timed_out"
                | "skipped"
                | "dry_run"
        )
    ) {
        return Err("spawner rejected or ended the run".into());
    }
    Err(format!(
        "spawner failed without an admitted run receipt (exit {exit:?})"
    ))
}
fn pin_local(cmd: &mut Command, dir: &Path) -> Result<(), String> {
    let path = dir.join("placement.json");
    crate::atomic::write(&path, br#"{"hosts":{},"public_repos":[]}"#)
        .map_err(|_| "cannot write local placement policy")?;
    cmd.env("PLANGRAPH_OVERFLOW_HOSTS", path);
    Ok(())
}

pub(super) struct Adapters<'a, T> {
    pub keeper: T,
    pub env: &'a Env,
    pub repo: PathBuf,
    pub state: PathBuf,
    pub host: String,
}
impl<T: Transport> Launch for Adapters<'_, T> {
    fn claim(&mut self, key: &str) -> Result<u64, ClaimError> {
        let result = self
            .keeper
            .call("claim?fmt=json", Some(json!({"key":key})))
            .map_err(|e| {
                if let Some(code) = e.strip_prefix("item_refused:") {
                    ClaimError::Refused(code.into())
                } else {
                    ClaimError::Unavailable(e)
                }
            })?;
        result["epoch"].as_u64().ok_or_else(|| {
            ClaimError::Unavailable("claim result missing epoch; reconcile claim".into())
        })
    }
    fn release(&mut self, key: &str, epoch: u64) -> Result<(), String> {
        self.keeper
            .call("release?fmt=json", Some(json!({"key":key,"epoch":epoch})))
            .map(|_| ())
    }
    fn rival(
        &mut self,
        key: &str,
        files: &[String],
        batch: &[String],
    ) -> Result<Option<String>, String> {
        if files.is_empty() {
            return Ok(None);
        }
        let snapshot = self.keeper.call("snapshot?fmt=json", None)?;
        let now = chrono::Utc::now();
        let others = snapshot["items"]
            .as_array()
            .ok_or("snapshot missing items")?;
        for other in others {
            let Some(name) = other["key"].as_str() else {
                continue;
            };
            let lapsed = other["lease_until"]
                .as_str()
                .and_then(|at| chrono::DateTime::parse_from_rfc3339(at).ok())
                .is_some_and(|at| at < now);
            let project = |k: &str| k.split_once(':').map(|(p, _)| p.to_owned());
            if project(name) != project(key)
                || name == key
                || batch.iter().any(|k| k == name)
                || other["state"] != "claimed"
                || lapsed
            {
                continue;
            }
            // A claim whose scope cannot be read is a rival: unknown files may overlap.
            let overlaps = decode_task(other).is_ok_and(|task| {
                task.files
                    .iter()
                    .any(|a| files.iter().any(|b| crate::paths::paths_overlap(a, b)))
            });
            if overlaps || decode_task(other).is_err() {
                return Ok(Some(name.to_owned()));
            }
        }
        Ok(None)
    }
    fn start(&mut self, card: &Value, brief: &str) -> Result<Value, String> {
        let dir = tempfile::Builder::new()
            .prefix("launch-")
            .tempdir_in(&self.state)
            .map_err(|_| "cannot create launch directory")?;
        let card_path = dir.path().join("card.json");
        let prompt_path = dir.path().join("brief.md");
        fs::write(&card_path, card.to_string()).map_err(|_| "cannot write card")?;
        fs::write(&prompt_path, brief).map_err(|_| "cannot write brief")?;
        let mut cmd = python(self.env, &self.repo);
        pin_local(&mut cmd, dir.path())?;
        cmd.args([
            "-m",
            "plangraph.spawner.cli",
            "spawn",
            "--role",
            "implement",
            "--card",
        ])
        .arg(format!("@{}", card_path.display()))
        .arg("--prompt-file")
        .arg(prompt_path)
        .args(["--wait", "0"]);
        let out = cmd.output().map_err(|_| "cannot execute spawner")?;
        let mut run = receipt(&out.stdout, out.status.code())?;
        if run["host"].as_str().is_none_or(str::is_empty) {
            run["host"] = json!(self.host);
        }
        if !run["started"].is_number() {
            run["started"] = json!(chrono::Utc::now().timestamp_millis() as f64 / 1000.0);
        }
        run["item_key"] = card["item_key"].clone();
        run["files"] = card["files"].clone();
        if card["item_keys"].is_array() {
            run["item_keys"] = card["item_keys"].clone();
        }
        if crate::atomic::write(&dir.path().join("receipt.json"), run.to_string().as_bytes())
            .is_err()
        {
            run["feeder_warning"] = json!("receipt persistence failed; claim retained");
        }
        let _ = dir.keep();
        Ok(run)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    #[test]
    fn python_adapter_uses_the_selected_checkout_before_installed_sources() {
        let d = tempfile::tempdir().unwrap();
        fs::create_dir_all(d.path().join("src/plangraph")).unwrap();
        let extra = d.path().join("extra");
        let env = Env::from([("PYTHONPATH".into(), extra.to_string_lossy().into())]);
        let cmd = python(&env, d.path());
        let path = cmd
            .get_envs()
            .find(|(k, _)| *k == "PYTHONPATH")
            .unwrap()
            .1
            .unwrap();
        assert_eq!(
            std::env::split_paths(path).collect::<Vec<_>>(),
            vec![d.path().join("src"), extra]
        );
    }
    #[test]
    fn feeder_placement_cannot_overflow_after_local_admission() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        let d = tempfile::tempdir().unwrap();
        let parent = d.path().join("parent.json");
        fs::write(&parent, r#"{"hosts":{"remote":{}}}"#).unwrap();
        let env = Env::from([(
            "PLANGRAPH_OVERFLOW_HOSTS".into(),
            parent.to_string_lossy().into(),
        )]);
        let script = r#"import json,os
from pathlib import Path
from plangraph.spawner.overflow import decide
c=json.loads(Path(os.environ['PLANGRAPH_OVERFLOW_HOSTS']).read_text())
rows={n:dict(ssh='fake',ok=True,fresh=True,live_runs=0,mem_available_mb=10000,role_mb=2000,floor_mb=3000) for n in c['hosts']}
print(json.dumps(dict(host=decide(100,2000,3000,rows,True).host)))
"#;
        let mut control = python(&env, root);
        control.args(["-c", script]);
        assert_eq!(output(control).unwrap()["host"], "remote");
        let mut local = python(&env, root);
        pin_local(&mut local, d.path()).unwrap();
        local.args(["-c", script]);
        assert!(output(local).unwrap()["host"].is_null());
    }
    #[test]
    fn keeper_response_distinguishes_merge_index_conflicts() {
        for (code, expected) in [
            ("merge_index_expired", MERGE_INDEX_EXPIRED),
            ("merge_index_running", MERGE_INDEX_RUNNING),
            ("conflict", "item_refused:conflict"),
        ] {
            let raw = format!("{}\n409", json!({"error":code,"msg":"reworded diagnostic"}));
            assert_eq!(http_response(raw.as_bytes()).unwrap_err(), expected);
        }
    }
    #[test]
    fn keeper_response_distinguishes_item_refusal_and_infrastructure() {
        assert_eq!(http_response(b"{\"epoch\":7}\n200").unwrap()["epoch"], 7);
        assert_eq!(
            http_response(b"{\"error\":\"bad_state\",\"msg\":\"private\"}\n409").unwrap_err(),
            "item_refused:bad_state"
        );
        assert!(!http_response(b"{\"error\":\"forbidden\"}\n403")
            .unwrap_err()
            .starts_with("item_refused:"));
        assert!(!http_response(b"{\"error\":\"bad_state\"}\n500")
            .unwrap_err()
            .starts_with("item_refused:"));
    }
    #[test]
    fn durable_nonzero_receipts_are_distinct_from_unadmitted_failures() {
        for (status, code) in [("queued", 5), ("unknown", 1), ("running", 0)] {
            let v = json!({"run_id":"run-a","status":status});
            assert_eq!(receipt(v.to_string().as_bytes(), Some(code)).unwrap(), v);
        }
        assert!(receipt(b"not JSON", Some(1)).is_err());
        assert!(receipt(b"parameter refusal", Some(2)).is_err());
        assert!(receipt(b"{\"run_id\":\"run-a\",\"status\":\"refused\"}", Some(2)).is_err());
    }
    struct Fake(RefCell<Vec<(String, Option<Value>)>>);
    impl Transport for Fake {
        fn call(&self, v: &str, b: Option<Value>) -> Result<Value, String> {
            self.0.borrow_mut().push((v.into(), b));
            Ok(json!({"epoch":19}))
        }
    }
    #[test]
    fn keeper_payloads_match_cli_and_release_claim_epoch() {
        let env = Env::new();
        let temp = tempfile::tempdir().unwrap();
        let mut a = Adapters {
            keeper: Fake(RefCell::new(vec![])),
            env: &env,
            repo: temp.path().into(),
            state: temp.path().into(),
            host: "dell".into(),
        };
        let epoch = a.claim("plangraph:A").unwrap();
        a.release("plangraph:A", epoch).unwrap();
        assert_eq!(
            *a.keeper.0.borrow(),
            vec![
                ("claim?fmt=json".into(), Some(json!({"key":"plangraph:A"}))),
                (
                    "release?fmt=json".into(),
                    Some(json!({"key":"plangraph:A","epoch":19}))
                )
            ]
        );
    }
    struct Snapshot(Value);
    impl Transport for Snapshot {
        fn call(&self, _: &str, _: Option<Value>) -> Result<Value, String> {
            Ok(self.0.clone())
        }
    }
    #[test]
    fn rival_is_a_live_claim_on_an_overlapping_file_outside_the_batch() {
        let env = Env::new();
        let temp = tempfile::tempdir().unwrap();
        let future = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();
        let past = (chrono::Utc::now() - chrono::Duration::hours(1)).to_rfc3339();
        let snapshot = json!({"items":[
            {"key":"plangraph:ME","state":"claimed","lease_until":future,"note":"files: [\"src/a.rs\"]"},
            {"key":"plangraph:MATE","state":"claimed","lease_until":future,"note":"files: [\"src/a.rs\"]"},
            {"key":"plangraph:LAPSED","state":"claimed","lease_until":past,"note":"files: [\"src/a.rs\"]"},
            {"key":"plangraph:OPEN","state":"open","note":"files: [\"src/a.rs\"]"},
            {"key":"plangraph:ELSEWHERE","state":"claimed","lease_until":future,"note":"files: [\"src/z.rs\"]"},
            {"key":"autoos:FOREIGN","state":"claimed","lease_until":future,"note":"files: [\"src/a.rs\"]"},
            {"key":"plangraph:THEIRS","state":"claimed","lease_until":future,"note":"files: [\"src/a.rs\"]"}]});
        let mut a = Adapters {
            keeper: Snapshot(snapshot),
            env: &env,
            repo: temp.path().into(),
            state: temp.path().into(),
            host: "dell".into(),
        };
        let files = vec!["src/a.rs".to_owned()];
        let batch = vec!["plangraph:ME".to_owned(), "plangraph:MATE".to_owned()];
        assert_eq!(
            a.rival("plangraph:ME", &files, &batch).unwrap().as_deref(),
            Some("plangraph:THEIRS")
        );
        assert_eq!(a.rival("plangraph:ME", &[], &batch).unwrap(), None);
        let batch = vec!["plangraph:ME".to_owned(), "plangraph:THEIRS".to_owned()];
        assert_eq!(
            a.rival("plangraph:ME", &files, &batch).unwrap().as_deref(),
            Some("plangraph:MATE")
        );
    }
    #[test]
    fn a_claim_with_unreadable_files_is_a_rival() {
        let env = Env::new();
        let temp = tempfile::tempdir().unwrap();
        let future = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();
        let snapshot = json!({"items":[
            {"key":"plangraph:BROKEN","state":"claimed","lease_until":future,"note":"files: {\"invalid\":true}"}]});
        let mut a = Adapters {
            keeper: Snapshot(snapshot),
            env: &env,
            repo: temp.path().into(),
            state: temp.path().into(),
            host: "dell".into(),
        };
        let files = vec!["src/a.rs".to_owned()];
        assert_eq!(
            a.rival("plangraph:ME", &files, &[]).unwrap().as_deref(),
            Some("plangraph:BROKEN")
        );
    }
}
