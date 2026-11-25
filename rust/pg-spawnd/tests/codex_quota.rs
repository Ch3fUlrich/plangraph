//! Exercise the production route and launch-spec adapters against a changing quota DB.
#[cfg(test)]
#[path = "../../test_support/python.rs"]
mod test_python;

#[test]
fn route_and_launch_read_new_codex_snapshot_before_cached_healthy_state_expires() {
    let state = tempfile::tempdir().unwrap();
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let output = test_python::command()
        .current_dir(&root)
        .env("PYTHONPATH", root.join("src"))
        .env("XDG_STATE_HOME", state.path())
        .env("PLANGRAPH_OMNIROUTE_LEG_STATE", "0")
        .env_remove("PLANGRAPH_ROUTE_POLICY")
        .env("QUOTA_SNAPSHOT_DB", state.path().join("quota.sqlite"))
        .args(["-c", r#"
import os, sqlite3, time
from contextlib import closing
from plangraph.spawner.launch_spec import build, LaunchRefused
from plangraph.spawner.service import SpawnService

state = os.environ['XDG_STATE_HOME']
database = os.environ['QUOTA_SNAPSHOT_DB']
now = int(time.time())

def write(sql, *args):
    # sqlite3's own `with` only commits; close so Windows can unlink the file.
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(sql, args)

reset = now + 3600
stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(reset))
write('CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY, provider, connection_id, window_key, '
      'remaining_percentage, is_exhausted, next_reset_at, created_at)')
write('INSERT INTO quota_snapshots VALUES (1, ?, ?, ?, 90, 0, ?, ?)', 'codex', 'a', 'weekly', stamp, now - 2)

service = SpawnService(state=state)
card = {'estimate_tokens': 1000}
initial = service.route(card)
assert initial.first_leg.id == 'cx-gpt-6.2-sol-medium', initial.to_dict()
write('INSERT INTO quota_snapshots VALUES (2, ?, ?, ?, 0, 1, ?, ?)', 'codex', 'a', 'weekly', stamp, now - 1)

plan = service.route(card)
assert plan.first_leg and not any(leg.id.startswith('cx-gpt-') for leg in plan.legs), [leg.id for leg in plan.legs]
skips = {row['leg']: row['reason'] for row in plan.skipped}
for leg in ('cx-gpt-6.2-sol-medium', 'cx-gpt-6.2-astra'):
    assert skips[leg] == 'quota_until ' + stamp, skips

# This request was built from the previous healthy plan (or an explicit pin).
try:
    build(dict(state=state, cwd=state, role='implement', dry_run=False,
               leg={'id': initial.first_leg.id}, card=card, prompt='run the card',
               run_id='run-' + 'a' * 32, job_id='fixture'))
except LaunchRefused as exc:
    assert 'quota_until ' + stamp in str(exc), exc
else:
    raise AssertionError('stale plan passed launch admission')

# The stated reset passes while the snapshot is still the last word: no clock brings the pool back.
write('UPDATE quota_snapshots SET next_reset_at=? WHERE id=2', now - 1)
pending = service.route(card)
assert not any(leg.id.startswith('cx-gpt-') for leg in pending.legs), [leg.id for leg in pending.legs]
skips = {row['leg']: row['reason'] for row in pending.skipped}
for leg in ('cx-gpt-6.2-sol-medium', 'cx-gpt-6.2-astra'):
    assert skips[leg] == 'quota_probe_pending', skips
try:
    build(dict(state=state, cwd=state, role='implement', dry_run=False,
               leg={'id': initial.first_leg.id}, card=card, prompt='run the card',
               run_id='run-' + 'b' * 32, job_id='fixture'))
except LaunchRefused as exc:
    assert 'quota_probe_pending' in str(exc), exc
else:
    raise AssertionError('launch admission passed after the reset without a probe')

# A successful probe newer than the snapshot is the recovery fact: route and launch admission return.
from plangraph.routing_support import leg_state
from plangraph.routing_support.liveness import require_available
from plangraph.planner.legs import DEFAULT_LEGS
chosen = next(leg for leg in DEFAULT_LEGS if leg.id == initial.first_leg.id)
leg_state.record_probe_pass(chosen.model, 'review-probe')
assert service.route(card).first_leg.id == initial.first_leg.id
require_available(chosen, service.leg_reader, card)
os.unlink(database)
assert service.route(card).first_leg.id == initial.first_leg.id
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
