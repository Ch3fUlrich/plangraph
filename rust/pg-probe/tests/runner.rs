use pg_probe::runner::{run_batch, Pinned};
use pg_probe::{RunConfig, SourceStatus};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fs};
#[test]
fn adapter_child() {
    let Some(path) = std::env::var_os("PG_PROBE_REQUEST_FILE") else {
        return;
    };
    let request: serde_json::Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert!(request.get("gold").is_none());
    assert!(request["config"]["fresh_session"].as_bool().unwrap());
    assert!(std::path::Path::new(&std::env::var_os("HOME").unwrap()).is_dir());
    assert!(std::env::var_os("GIT_DIR").is_none());
    fs::write(std::env::var_os("PG_PROBE_RESPONSE_FILE").unwrap(),
        r#"{"final_answer":"D-653","cited_ids":["D-653"],"tool_calls":[{"tool":"context","file_class":null}]}"#).unwrap();
}
#[test]
fn pinned_runner_records_raw_responses_and_rejects_changed_adapter() {
    let tmp = tempfile::tempdir().unwrap();
    let exe = std::env::current_exe().unwrap();
    let hash = format!("{:x}", Sha256::digest(fs::read(&exe).unwrap()));
    let mut pinned = Pinned {
        config: RunConfig {
            leg: "fake".into(),
            fresh_session: true,
            tool_set: vec!["context".into()],
            repo_sha: "a".repeat(40),
            no_memory_dir: true,
        },
        adapter: exe,
        adapter_sha256: hash,
        args: vec!["--exact".into(), "adapter_child".into()],
        timeout_seconds: 5,
        concurrency: 4,
    };
    let probes = pg_probe::load(std::io::Cursor::new(include_str!(
        "../fixtures/probes.jsonl"
    )))
    .unwrap();
    let sources = BTreeMap::from([("D-653".into(), SourceStatus::InForce)]);
    let rows = run_batch(&probes, &sources, &pinned, tmp.path()).unwrap();
    assert_eq!(rows.len(), probes.len());
    assert_eq!(rows[0].response.final_answer, "D-653");
    assert_eq!(rows[0].score.answer_accuracy, 1.0);
    assert_eq!(rows[0].adapter_sha256, pinned.adapter_sha256);
    let mut changed = probes.clone();
    changed[0].gold = "different gold".into();
    let rescored = pg_probe::runner::rescore(&changed, &sources, &pinned.config, &rows).unwrap();
    assert_eq!(rescored[0].score.answer_accuracy, 0.0);
    assert_eq!(rescored[0].response, rows[0].response);
    pinned.adapter_sha256 = "0".repeat(64);
    assert!(run_batch(&probes, &sources, &pinned, tmp.path()).is_err());
    pinned.concurrency = 5;
    assert!(run_batch(&probes, &sources, &pinned, tmp.path()).is_err());
}
