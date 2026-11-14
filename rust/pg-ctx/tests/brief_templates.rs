//! Exercise the production prompt builders across the existing Python boundary.
use std::path::Path;
use std::process::Command;

fn render(script: &str) -> serde_json::Value {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .args(["-c", script])
        .env("PYTHONPATH", root.join("src"))
        .current_dir(root)
        .output()
        .expect("run production renderer");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("rendered briefs")
}

#[test]
fn review_builders_carry_item_intent_and_evidence() {
    let prompts = render(
        r#"
import json
from plangraph.review.prompt import assemble, assemble_all, assemble_diff
from tools.judge_prompt import build_judge_prompt
ctx = dict(item_key='plangraph:FIXTURE', done_criteria='fixture completes',
           invariants='preserve ordering', base_sha='a' * 40, head_sha='b' * 40,
           public_symbols='api::run', callers='cli::main', test_output_paths='artifacts/unit.txt')
judge = build_judge_prompt('F1', 'diff', {}, {}, context=ctx)
print(json.dumps([assemble(['correctness'], [('diff', 'body')], context=ctx),
                  *assemble_all(['check'], {'one.rs': 'body'}, context=ctx),
                  *assemble_diff(['check'], '', context=ctx),
                  judge, judge.with_tool_mode('tools')]))
"#,
    );
    for prompt in prompts.as_array().unwrap() {
        let text = prompt.as_str().unwrap();
        for field in [
            "Item key: plangraph:FIXTURE",
            "Done criteria: fixture completes",
            "Invariants named by the item: preserve ordering",
            &format!("Exact base SHA: {}", "a".repeat(40)),
            &format!("Exact head SHA: {}", "b".repeat(40)),
            "Changed public symbols: api::run",
            "Callers of changed public symbols: cli::main",
            "Affected test output (paths): artifacts/unit.txt",
            "The reviewer may read the repository",
        ] {
            assert!(text.contains(field), "missing {field}: {text}");
        }
        assert_eq!(text.matches("=== ITEM CONTEXT").count(), 1);
    }
}

#[test]
fn different_items_share_the_complete_rules_prefix() {
    let briefs = render(
        r#"
import json
from plangraph.review.prompt import render_brief, assemble
from plangraph.routing_support.fixer import render_fixer_brief
from tools.judge_prompt import build_judge_prompt
pairs = []
for builder in (lambda c: render_brief('writer', c),
                lambda c: render_brief('review', c),
                lambda c: render_fixer_brief([{'text': c['done_criteria']}], c),
                lambda c: assemble([c['done_criteria']], [('diff', c['item_key'])], context=c),
                lambda c: build_judge_prompt(c['done_criteria'], c['item_key'], {}, {}, context=c)):
    pairs.append([builder(dict(item_key='plangraph:' + key, done_criteria='finish ' + key))
                  for key in ('FIRST', 'SECOND')])
print(json.dumps(pairs))
"#,
    );
    for pair in briefs.as_array().unwrap() {
        let first = pair[0].as_str().unwrap();
        let second = pair[1].as_str().unwrap();
        let prefix = first.split_once("=== ITEM CONTEXT").unwrap().0;
        assert_eq!(
            prefix.as_bytes(),
            second.split_once("=== ITEM CONTEXT").unwrap().0.as_bytes()
        );
        for field in [
            "Role:",
            "Rules:",
            "Language:",
            "Gates:",
            "Plan-Item:",
            "Writer:",
        ] {
            assert!(prefix.contains(field), "prefix missing {field}");
        }
        assert!(!prefix.contains("FIRST"));
        assert!(!prefix.contains("SECOND"));
        assert!(first.contains("plangraph:FIRST"));
        assert!(second.contains("plangraph:SECOND"));
        assert_ne!(first, second);
    }
}
