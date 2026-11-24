//! A landed batch whose parts are cited on the native review records of the spawner closes without a
//! `seats_missing` flag: the records are written by the real run path, the citations are `Review-Run:`
//! trailers of the batch merge, and the close is the keeper's automatic close.
use super::tests::{finish_agy_review, git_ok};
use super::*;
use crate::test_python;

const CLOSE: &str = r#"
import json, sys
from pathlib import Path
root, repo = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(Path.cwd() / 'tests'))
from keeper_util import World, defn, to_in_review
from plangraph.keeper.evidence_git import GitAdapter
from plangraph.keeper.evidence_spawner import SpawnerRecordAdapter
(root / 'w').mkdir()
w = World(root / 'w', backend='sqlite')
w.store.require_seats = True
w.store.create_items(w.p('router'), [defn('A', review_tier='standard'), defn('B', review_tier='standard'),
    defn('G', ['A', 'B'], kind='goal')])
for key in 'AB':
    to_in_review(w, f'demo:{key}')
w.store.ctx = {'git': GitAdapter({'demo': repo}), 'spawner_records': SpawnerRecordAdapter(root / 'mirror')}
print(json.dumps(w.store.close_merged_auto(w.p('system'))))
w.close()
"#;

fn commit(dir: &Path, message: &str) -> String {
    git_ok(
        dir,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        ],
    );
    git(dir, &["rev-parse", "HEAD"]).ok().unwrap()
}

#[test]
fn a_landed_batch_cited_on_native_batch_review_records_closes_without_seats_missing() {
    let work = tempfile::tempdir().unwrap();
    let repo = work.path().join("repo");
    std::fs::create_dir(&repo).unwrap();
    git_ok(&repo, &["init", "-q", "-b", "main"]);
    commit(&repo, "base");
    let mut trailers = Vec::new();
    git_ok(&repo, &["checkout", "-qb", "batch"]);
    for (number, key) in [(1, "A"), (2, "B")] {
        git_ok(&repo, &["checkout", "-qb", &format!("part{key}"), "main"]);
        let head = commit(&repo, &format!("part {key}\n\nPlan-Item: demo:{key}\n"));
        git_ok(&repo, &["checkout", "-q", "batch"]);
        git_ok(
            &repo,
            &[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "merge",
                "--no-ff",
                "-q",
                "-m",
                &format!("Merge PR #{number}: part {key}"),
                &format!("part{key}"),
            ],
        );
        // The native review of the part head: one record for both parts of the batch card.
        let checkout = work.path().join(format!("review-{key}"));
        git_ok(
            &repo,
            &[
                "worktree",
                "add",
                "-q",
                "--detach",
                checkout.to_str().unwrap(),
                &head,
            ],
        );
        let state = work.path().join(format!("state-{key}"));
        std::fs::create_dir(&state).unwrap();
        let (record, id) = finish_agy_review(
            &state,
            &checkout,
            &head,
            "HEAD: {head}\nTESTS: none: no shell\nFILES READ: src/lib.rs:1\nTEST_GAMING: no\nVERDICT: ACCEPT\n",
            &json!({"item_keys":["demo:A", "demo:B"]}),
        );
        let value = record.to_value();
        assert_eq!(value["item_key"], Value::Null, "{value}");
        assert_eq!(value["completion"]["head_sha"], head);
        record.save().unwrap();
        let mirror = work.path().join("mirror/fixture").join(&id);
        std::fs::create_dir_all(&mirror).unwrap();
        {
            use std::os::unix::fs::PermissionsExt;
            for path in [mirror.clone(), mirror.parent().unwrap().into()] {
                std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
            }
        }
        std::fs::copy(record.path(), mirror.join("record.json")).unwrap();
        trailers.push(format!(
            "Plan-Item: demo:{key}\nReview-Run: demo:{key} {id} {head}"
        ));
    }
    git_ok(&repo, &["checkout", "-q", "main"]);
    git_ok(
        &repo,
        &[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "merge",
            "--no-ff",
            "-q",
            "-m",
            &format!(
                "Merge pull request #100 from x/batch\n\n{}",
                trailers.join("\n")
            ),
            "batch",
        ],
    );
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args(["-c", CLOSE])
        .arg(work.path())
        .arg(&repo)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    let mut closed: Vec<_> = result["closed"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["key"].as_str().unwrap().to_owned())
        .collect();
    closed.sort();
    assert_eq!(closed, ["demo:A", "demo:B"], "{result}");
    assert_eq!(result["listed"], json!([]), "{result}");
    assert!(!result.to_string().contains("seats_missing"), "{result}");
}
