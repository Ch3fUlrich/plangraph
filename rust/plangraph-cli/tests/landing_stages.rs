#![cfg(unix)]

use serde_json::{json, Value};
use std::{path::Path, process::Command};

#[test]
fn keeper_stage_rows_survive_retry_and_upgrade_in_timeline_order() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let temp = tempfile::tempdir().unwrap();
    let stages = [
        "writing",
        "review",
        "fast_checks",
        "push",
        "ci",
        "delta_review",
        "go",
        "merge",
        "host_sync",
    ];
    let head = "a".repeat(40);
    let mut rows: Vec<_> = stages
        .iter()
        .enumerate()
        .map(|(i, stage)| {
            json!([
                "example/repo",
                "train-1",
                stage,
                1,
                head,
                i * 10,
                i * 10 + 5
            ])
        })
        .collect();
    rows.push(json!([
        "example/repo",
        "train-1",
        "host_sync",
        2,
        head,
        100,
        105
    ]));
    let fixture = r#"
import json, sqlite3, sys
from importlib.resources import files
from plangraph.keeper.db import open_db
from plangraph.keeper.copy_to_pg import _open_source

path, encoded = sys.argv[1:]
rows = json.loads(encoded)
open_db(path, create=True, backend="sqlite").close()
with sqlite3.connect(path) as before:
    before.execute("DROP TABLE landing_stages")
conn = open_db(path, backend="sqlite")
sql = files("plangraph.keeper")
insert = sql.joinpath("landing_stage_insert.sql").read_text(encoding="utf-8")
for row in reversed(rows):
    conn.execute(insert, row)
duplicate = rows[0][:4] + ["b" * 40, 500, 600]
conn.execute(insert, duplicate)
conn.execute(insert, ["example/repo", "train-2", *rows[0][2:]])
conn.execute(insert, ["other/repo", "train-1", *rows[0][2:]])
conn.close()
source, tables = _open_source(path)
assert "landing_stages" in tables
query = sql.joinpath("landing_stage_list.sql").read_text(encoding="utf-8")
result = source.execute(query, ("example/repo", "train-1"))
print(json.dumps([tuple(row) for row in result]))
"#;
    let output = Command::new("python3")
        .env("PYTHONPATH", repo.join("src"))
        .arg("-c")
        .arg(fixture)
        .arg(temp.path().join("keeper.db"))
        .arg(serde_json::to_string(&rows).unwrap())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let actual: Vec<Value> = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(actual, rows);
}
