use super::disk_bridge_with;

#[test]
fn cap_cleanup_ignores_dead_records_and_ps_marks_dead_running_record_stale() {
    // No process-liveness proof exists off Unix (run_pids_dead), so a `running` record is
    // never dead there and keeps the cache busy; `timed_out` and `stalled` need no proof.
    let statuses = if cfg!(unix) {
        "timed_out,stalled,running"
    } else {
        "timed_out,stalled"
    };
    let result = disk_bridge_with(
        statuses,
        r#"
import json, os, subprocess, sys
from pathlib import Path
from plangraph.spawner import host_ready as h
from plangraph.spawner.service import SpawnService
from types import SimpleNamespace

root = Path(sys.argv[1])
dead = subprocess.Popen([sys.executable, '-c', 'pass'])
dead.wait()
records = [dict(status=status, pid=dead.pid, run_id=f'run-{index}') for index, status in enumerate(sys.argv[2].split(','))]
service = SpawnService(state=root / 'state')
for record in records:
    service._save(record)
target = h.shared_cargo_target()
target.mkdir()
(target / 'artifact').write_bytes(b'x' * 4096)
os.environ['PLANGRAPH_CARGO_TARGET_CAP_GB'] = '0.000001'
h.disk_free_gb = lambda path: 41
print(json.dumps(dict(runs=service.ps()['runs'], refusal=h.local_disk_refusal(root / 'checkout', service), reclaimed=not target.exists())))
"#,
    );
    assert_eq!(result["reclaimed"], true);
    assert!(result["refusal"].is_null(), "{result}");
    let running = result["runs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["status"] == "running");
    if cfg!(unix) {
        assert_eq!(running.unwrap()["stale"], true);
    } else {
        assert!(running.is_none());
    }
}
