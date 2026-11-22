#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

#[test]
fn agy_limit_admission_distinguishes_account_and_model() {
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let state = tempfile::tempdir().unwrap();
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .env_remove("QUOTA_SNAPSHOT_DB")
        .args(["-c", r#"
import os, time
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support.leg_state import record_harness_limit
from plangraph.spawner.launch_spec import build, LaunchRefused

state = os.environ['XDG_STATE_HOME']
card = {'estimate_tokens': 1000}
argon = 'agy-gemini-4-argon-low'
def route(now, account='primary'):
    return plan_spawn(dict(card, account=account), role='review', writer_families=['gpt'],
                      catalog_rows=[], now=now).to_dict()

now = time.time() - 1
record_harness_limit('agy/gemini-4-hydrogen-high', '429 Per-model quota reached. Resets in 2h',
                     'fixture', now=now, account='primary')
def served(plan):
    return [leg['id'] for leg in plan['legs']]

# Rule 3.61 puts argon last in the review order: it is served (listed), not first.
model = route(now)
assert argon in served(model), model
assert model['agy_quota_states']['agy:gemini-4-hydrogen']['evidence']['reset_at'] == now + 7200
record_harness_limit('agy/gemini-4-hydrogen-high', '429 Individual quota reached. Resets in 3h',
                     'fixture', now=now, account='primary')
account = route(now)
assert not any(leg['id'].startswith('agy-') for leg in account['legs']), account
assert next(s for s in account['skipped'] if s['leg'] == argon)['reason'].startswith('limited_until ')
assert argon in served(route(now, 'secondary'))
assert argon in served(route(now + 10800))

# A sibling was selected in an earlier plan, before the failure was booked.
try:
    build(dict(state=state, cwd=state, role='review', dry_run=False, leg={'id': argon},
               card=dict(card, account='primary'), prompt='review the card',
               run_id='run-' + 'a'*32, job_id='fixture', writer_families=['gpt']))
except LaunchRefused as exc:
    assert 'limited_until ' in str(exc), exc
else:
    raise AssertionError('stale route passed launch admission')
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
