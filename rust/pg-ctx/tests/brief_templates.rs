//! Exercise the production prompt builders across the existing Python boundary.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

use std::path::Path;

fn render(script: &str) -> serde_json::Value {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
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
print(json.dumps({
    'inline-review': [
        assemble(['correctness'], [('diff', 'body')], context=ctx),
        *assemble_all(['check'], {'one.rs': 'body'}, context=ctx),
        *assemble_diff(['check'], '', context=ctx),
        *assemble_diff(['check'], 'diff --git a/one.rs b/one.rs\n--- a/one.rs\n+++ b/one.rs\n@@ -1 +1 @@\n-old\n+new\n', context=ctx),
    ],
    'prompt-only-judge': [judge],
    'inline-only-judge': [judge.with_tool_mode('inline-only')],
    'tools-judge': [judge.with_tool_mode('tools')],
}))
"#,
    );
    let context_fields = [
        "Item key: plangraph:FIXTURE",
        "Done criteria: fixture completes",
        "Invariants named by the item: preserve ordering",
        &format!("Exact base SHA: {}", "a".repeat(40)),
        &format!("Exact head SHA: {}", "b".repeat(40)),
        "Changed public symbols: api::run",
        "Callers of changed public symbols: cli::main",
        "Affected test output (paths): artifacts/unit.txt",
    ];
    for (mode, capability) in [
        (
            "inline-review",
            "You have NO tools and no repository access.",
        ),
        (
            "prompt-only-judge",
            "Cut files are unavailable; judge only supplied evidence. No tools are available.",
        ),
        (
            "inline-only-judge",
            "Read only the spawner-inlined diff. No tools are available.",
        ),
        (
            "tools-judge",
            "You may Read/Grep/Glob for cut files; deleted files may remain unavailable.",
        ),
    ] {
        for prompt in prompts[mode].as_array().unwrap() {
            let text = prompt.as_str().unwrap();
            let (rules, context) = text.split_once("=== ITEM CONTEXT").unwrap();
            assert!(
                rules.contains(capability),
                "{mode} missing {capability}: {text}"
            );
            if mode != "tools-judge" {
                assert!(!rules.contains("Read/Grep/Glob"), "{mode}: {rules}");
                assert!(
                    !rules.contains("The reviewer may read the repository"),
                    "{mode}: {rules}"
                );
                assert!(!rules.contains("CARGO_TARGET_DIR"), "{mode}: {rules}");
            }
            for field in context_fields {
                assert!(context.contains(field), "{mode} missing {field}: {text}");
            }
            assert!(!context.contains("=== ITEM CONTEXT"));
            assert!(!text.contains("Read files and run tests"));
        }
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

#[test]
fn judge_renders_in_an_uninstalled_checkout() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .args(["-c", "from tools.judge_prompt import build_judge_prompt; print(build_judge_prompt('F1', 'diff', {}, {}))"])
        .env_remove("PYTHONPATH")
        .current_dir(root)
        .output()
        .expect("run standalone judge");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("=== ITEM CONTEXT"));
}
