//! Exercise Python launch identity until the runner is ported to Rust.
use serde_json::Value;
use std::process::Command;

#[test]
fn job_session_tags_keep_uuid_entropy() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let state = tempfile::tempdir().unwrap();
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .arg("-c")
        .arg(
            r#"
import asyncio, itertools, json, sys, uuid
from unittest.mock import patch
from plangraph.spawner import gateway_witness, runner
ids = (uuid.UUID(f'12345678-0000-4000-8000-{i:012x}') for i in itertools.count(1))
async def launch():
    owner = runner.SpawnerRunner(default_cwd=sys.argv[1])
    with patch.object(runner.uuid, 'uuid4', side_effect=lambda: next(ids)):
        jobs = [await owner.spawn('prompt', model_or_combo='omniroute/cx-gpt-6-luna',
                                  client='opencode', dry_run=True) for _ in range(2)]
    return [{'tag': gateway_witness.session_tag(job.job_id), 'command': job.command} for job in jobs]
print(json.dumps(asyncio.run(launch())))
"#,
        )
        .arg(state.path())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let tags: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(tags.len(), 2);
    assert_ne!(
        tags[0]["tag"], tags[1]["tag"],
        "launches with equal UUID prefixes must differ"
    );
    let contract: Value = serde_json::from_slice(
        &std::fs::read(root.join("src/plangraph/config/prompt_transport_v1.json")).unwrap(),
    )
    .unwrap();
    let metadata = &contract["metadata"];
    let pattern = regex::Regex::new(metadata["title_pattern"].as_str().unwrap()).unwrap();
    for job in tags {
        let command = job["command"].as_array().unwrap();
        let title = command
            .windows(2)
            .find(|pair| pair[0] == "--title")
            .unwrap()[1]
            .as_str()
            .unwrap();
        assert_eq!(title, job["tag"].as_str().unwrap());
        assert_eq!(
            title.len() as u64,
            metadata["title_ascii_bytes"].as_u64().unwrap()
        );
        assert!(pattern.is_match(title));
    }
}
