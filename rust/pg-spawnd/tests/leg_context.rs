use std::process::Command;

fn compare_gateway_context(
    config: &serde_json::Value,
    models: &serde_json::Value,
) -> Result<(), String> {
    for model in models["data"].as_array().into_iter().flatten() {
        let Some(id) = model["id"].as_str() else {
            continue;
        };
        let Some(base) = ["cx/gpt-6.2-sol", "cx/gpt-6.2-astra"]
            .into_iter()
            .find(|base| id == *base || id.strip_prefix(base).is_some_and(|s| s.starts_with('-')))
        else {
            continue;
        };
        let key = base.replace('/', "-");
        let expected = &config["providers"]["omniroute"]["models"][key]["limit"]["context"];
        if model["context_length"] != *expected {
            return Err(format!(
                "{id}: gateway context {} != providers.json {expected}",
                model["context_length"]
            ));
        }
    }
    Ok(())
}

#[test]
fn gateway_context_drift_names_the_variant() {
    let config = serde_json::from_str(include_str!(
        "../../../configuration/opencode/providers.json"
    ))
    .unwrap();
    let id = "cx/gpt-6.2-sol-medium";
    let models = |context| serde_json::json!({"data":[{"id":id,"context_length":context}]});
    assert!(compare_gateway_context(&config, &models(1_000_000)).is_ok());
    assert!(compare_gateway_context(&config, &models(872_000))
        .unwrap_err()
        .contains(id));
}

#[test]
fn large_card_uses_leg_context_not_the_window_label() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = Command::new("python3")
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .args([
            "-c",
            r#"
from dataclasses import replace
from pathlib import Path
from plangraph.planner.routing import plan_spawn
from plangraph.planner.legs import DEFAULT_LEGS, LEAD_WRITER

leg = next(l for l in DEFAULT_LEGS if l.id == LEAD_WRITER)
small = replace(leg, id='small', windows=('128k', '256k', '1m'), max_request_tokens=262144)
card = {'estimate_tokens': 500000, 'window_class': '256k'}
plan = plan_spawn(card, role='implement', legs=(small, leg), catalog_rows=[],
                  leg_reader=lambda *a, **kw: dict(state='ok', evidence=None))
assert plan.first_leg == leg, (plan.error, plan.skipped)
small_skip = next(s for s in plan.skipped if s['leg'] == 'small')
assert small_skip['reason'] == 'max_request_tokens', small_skip
assert str(small.max_request_tokens) in small_skip['evidence']['error'], small_skip
assert sum(line.startswith('WINDOW_TOKENS =') for path in Path('src/plangraph/planner').glob('*.py')
           for line in path.read_text().splitlines()) == 1
"#,
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
