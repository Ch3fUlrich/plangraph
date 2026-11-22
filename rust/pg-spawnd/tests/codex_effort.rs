use std::process::Command;

#[test]
fn gpt_review_defaults_to_medium_and_explicit_high_requires_a_decision() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let state = tempfile::tempdir().unwrap();
    let output = Command::new("python3")
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .env_remove("QUOTA_SNAPSHOT_DB")
        .args(["-c", r#"
import os
from plangraph.planner.routing import plan_spawn
from plangraph.spawner.launch_spec import build, LaunchRefused

state = os.environ['XDG_STATE_HOME']
card = {'estimate_tokens': 1000}
writer = plan_spawn(card, role='implement', catalog_rows=[]).first_leg
assert (writer.id, writer.effort) == ('cx-gpt-6.2-sol-medium', 'medium'), writer
review = plan_spawn(dict(card, diff_ref=dict(repo=os.getcwd(), base='a'*40, head='b'*40)), role='review',
                    writer_families=['claude'], catalog_rows=[])
assert review.first_leg.id == 'agy-gemini-4-hydrogen-high', review.to_dict()
sol = review.legs[1]
assert (sol.id, sol.harness, sol.effort) == ('gpt-6.2-sol-medium', 'codex', 'medium'), sol
luna = next(leg for leg in review.legs if leg.id == 'cx-gpt-6-luna')
assert luna.effort == 'medium', luna
for effort in ('high', 'xhigh'):
    for options in ({}, {'effort': effort}, {'effort_decision': 'D-6397'}):
        try:
            build(dict(state=state, cwd=state, role='review', dry_run=True,
                       leg={'id': 'cx-gpt-6-luna-' + effort}, card=dict(card, **options),
                       prompt='review the card', run_id='run-' + 'a'*32, job_id='fixture',
                       writer_families=['claude']))
        except LaunchRefused as exc:
            assert exc.kind == 'effort_needs_decision', (exc.kind, str(exc))
        else:
            raise AssertionError('unapproved effort passed launch admission')
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
