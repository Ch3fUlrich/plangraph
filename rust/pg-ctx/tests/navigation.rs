use scip::types::{Document, Index, Occurrence, Relationship, SymbolInformation};
use serde_json::Value;
use std::path::Path;
use std::process::{Command, Output};

fn checked(output: Output) -> Vec<u8> {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

fn git(root: &Path, args: &[&str]) -> String {
    let output = checked(
        Command::new("git")
            .current_dir(root)
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
            .args(args)
            .output()
            .unwrap(),
    );
    String::from_utf8(output).unwrap().trim().to_owned()
}

fn nav(root: &Path, args: &[&str]) -> Value {
    let binary = env!("CARGO_BIN_EXE_pg-ctx");
    let output = checked(
        Command::new(binary)
            .current_dir(root)
            .args(args)
            .output()
            .unwrap(),
    );
    serde_json::from_slice(&output).unwrap()
}

fn occurrence(symbol: &str, range: Vec<i32>, symbol_roles: i32) -> Occurrence {
    Occurrence {
        symbol: symbol.into(),
        range,
        symbol_roles,
        ..Occurrence::default()
    }
}

fn write_scip(path: &Path) {
    const SCIP_UTF16: i32 = 2;
    let widget = "scip-rust cargo demo 0.1.0 widget().";
    let other = "scip-rust cargo demo 0.1.0 other/widget().";
    let client = "scip-rust cargo demo 0.1.0 client().";
    let index = Index {
        documents: vec![Document {
            relative_path: "code.rs".into(),
            position_encoding: protobuf::EnumOrUnknown::from_i32(SCIP_UTF16),
            occurrences: vec![
                occurrence(widget, vec![1, 3, 9], 1),
                occurrence(client, vec![2, 3, 9], 1),
                occurrence(widget, vec![2, 23, 29], 0),
                occurrence(widget, vec![2, 41, 47], 0),
                occurrence(other, vec![3, 15, 21], 1),
                occurrence(other, vec![3, 34, 40], 0),
            ],
            symbols: vec![SymbolInformation {
                symbol: other.into(),
                relationships: vec![Relationship {
                    symbol: widget.into(),
                    is_implementation: true,
                    ..Relationship::default()
                }],
                ..SymbolInformation::default()
            }],
            ..Document::default()
        }],
        ..Index::default()
    };
    scip::write_message_to_file(path, index).unwrap();
}

#[test]
fn navigates_a_committed_fixture_and_reports_stale_paths() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    git(root, &["init", "-q"]);
    std::fs::create_dir(root.join("docs")).unwrap();
    std::fs::create_dir(root.join("router")).unwrap();
    std::fs::write(
        root.join("code.rs"),
        "/// Finds a widget.\nfn widget() {}\nfn client() { /*🛠*/   widget(); let _ = widget; }\nmod other { fn widget() {} fn a(){widget();} }\n",
    )
    .unwrap();
    std::fs::write(
        root.join("docs/usage.md"),
        "# Usage\nCall `code.rs::widget`.\n",
    )
    .unwrap();
    std::fs::write(
        root.join("router/DECISIONS.md"),
        "# Rules\n## D-10 Widgets\nUse `code.rs::widget`.\n\n## Decisions\n\n| ID | Project | Decision |\n|---|---|---|\n| D-11 | demo | Document `code.rs::client`. |\n",
    )
    .unwrap();
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "fixture"]);
    let commit = git(root, &["rev-parse", "HEAD"]);
    let links = root.join("links.json");
    std::fs::write(
        &links,
        r#"[{"key":"demo:WIDGET","refs":["code.rs::widget","D-10"]}]"#,
    )
    .unwrap();

    let scip = root.join("fixture.scip");
    write_scip(&scip);
    nav(
        root,
        &[
            "nav-build",
            "--links",
            links.to_str().unwrap(),
            "--scip",
            scip.to_str().unwrap(),
        ],
    );
    let result = nav(root, &["nav", "demo:WIDGET"]);
    assert_eq!(result["index_commit"], commit);
    assert_eq!(result["stale"], false);
    assert_eq!(result["symbols"][0]["qualified_name"], "widget");
    assert_eq!(result["symbols"][0]["location"]["start_line"], 2);
    assert_eq!(result["symbols"][0]["location"]["end_line"], 2);
    assert_eq!(result["symbols"][0]["doc_comment"], "Finds a widget.");
    assert_eq!(result["sections"][0]["title"], "Usage");
    assert_eq!(result["rules"][0]["title"], "D-10 Widgets");
    assert_eq!(result["items"][0]["key"], "demo:WIDGET");
    assert_eq!(result["last_commits"][0]["commit"], commit);
    assert_eq!(result["symbols"].as_array().unwrap().len(), 1);
    assert_eq!(result["references"].as_array().unwrap().len(), 2);
    assert_eq!(result["callers"].as_array().unwrap().len(), 1);
    assert_eq!(result["callers"][0]["caller"]["start_line"], 3);
    assert_eq!(result["implementations"][0]["location"]["start_line"], 4);
    let outgoing = nav(root, &["nav", "client"]);
    assert_eq!(outgoing["callees"].as_array().unwrap().len(), 1);
    assert_eq!(outgoing["callees"][0]["definitions"][0]["start_line"], 2);
    let decision = nav(root, &["nav", "D-11"]);
    assert_eq!(decision["symbols"][0]["qualified_name"], "client");
    assert!(decision["rules"]
        .as_array()
        .unwrap()
        .iter()
        .any(|rule| rule["title"] == "D-11"));

    std::fs::write(root.join("docs/usage.md"), "# Changed\n").unwrap();
    git(root, &["add", "docs/usage.md"]);
    git(root, &["commit", "-qm", "update docs"]);
    let result = nav(root, &["nav", "code.rs::widget"]);
    assert_eq!(result["stale"], true);
    assert_eq!(
        result["changed_files"],
        serde_json::json!(["docs/usage.md"])
    );
}

#[test]
fn finds_qualified_names_across_supported_grammars() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path();
    git(root, &["init", "-q"]);
    for (path, content) in [
        ("code.py", "class Tool:\n    def run(self):\n        \"\"\"Run a workflow.\"\"\"\n        return True\n"),
        ("code.ts", "class Tool { run(): boolean { return true; } }"),
        ("code.js", "export const run = () => { return true; };"),
        ("runner", "#!/usr/bin/env bash\nrun() { echo ready; }\n"),
        ("AGENTS.md", "# Navigation\n## Setup\nRun `runner::run`.\n"),
        ("README.md", "Guide\n=====\n\n# Test\n\n```md\n# Example\n```\n"),
    ] {
        std::fs::write(root.join(path), content).unwrap();
    }
    git(root, &["add", "."]);
    git(root, &["commit", "-qm", "fixture"]);
    nav(root, &["nav-build"]);
    for (query, qualified_name) in [
        ("code.py::Tool::run", "Tool::run"),
        ("code.ts::Tool::run", "Tool::run"),
        ("code.js::run", "run"),
        ("runner::run", "run"),
        ("README.md::Guide", "Guide"),
    ] {
        let result = nav(root, &["nav", query]);
        assert_eq!(
            result["symbols"][0]["qualified_name"], qualified_name,
            "{query}"
        );
    }
    let python = nav(root, &["nav", "code.py::Tool::run"]);
    assert_eq!(python["symbols"][0]["doc_comment"], "Run a workflow.");
    let runner = nav(root, &["nav", "runner::run"]);
    assert_eq!(runner["sections"][0]["title"], "Setup");
    let fenced = nav(root, &["nav", "Example"]);
    assert_eq!(fenced["symbols"], serde_json::json!([]));
}
