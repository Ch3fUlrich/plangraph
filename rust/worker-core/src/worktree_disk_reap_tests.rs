// Exercise the existing Python e2e harness until its Rust port.
#[test]
fn teardown_ignores_cache_environment_and_reaps_recorded_children() {
    let result = super::disk_bridge(
        r#"
import json, os, subprocess, sys
import plangraph
from pathlib import Path
sys.path.insert(0, str(Path(plangraph.__file__).resolve().parents[2] / 'tests/e2e'))
import rtp1_local_util as u

root = Path(sys.argv[1])
root.mkdir(exist_ok=True)
foreign = subprocess.Popen(['sleep', '60'], env=dict(os.environ,
    PLANGRAPH_CARGO_TARGET_DIR=str(root) + '-cargo-target'))
owned = []
try:
    (root / str(foreign.pid)).write_text(str(foreign.pid))
    skipped = u.reap_recorded(root, grace_s=.01)
    foreign_alive = foreign.poll() is None
    # Only the harness's explicit PID-directory variable proves environment ownership.
    for index in range(2):
        proc = subprocess.Popen(['sleep', '60'], env=dict(os.environ,
            PG_FAKE_PID_DIR=str(root)))
        owned.append(proc)
        (root / str(proc.pid)).write_text(str(proc.pid))
    reaped = u.reap_recorded(root, grace_s=.01)
    for proc in owned:
        proc.wait(timeout=2)
    print(json.dumps(dict(skipped=skipped, foreign_alive=foreign_alive,
        reaped=sorted(reaped), expected=sorted(proc.pid for proc in owned))))
finally:
    for proc in [foreign, *owned]:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
"#,
    );
    assert_eq!(result["skipped"], serde_json::json!([]));
    assert_eq!(result["foreign_alive"], true);
    assert_eq!(result["reaped"], result["expected"]);
}
