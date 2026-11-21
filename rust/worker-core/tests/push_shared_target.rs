use std::process::Command;

#[test]
fn preflight_overrides_private_target_and_holds_shared_lease() {
    let repo = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let tmp = tempfile::tempdir().unwrap();
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .current_dir(&repo)
        .env("PYTHONPATH", repo.join("src"))
        .env("PLANGRAPH_CARGO_TARGET_DIR", tmp.path().join("shared"))
        .env("CARGO_TARGET_DIR", tmp.path().join("private"))
        .args(["-c", r#"
import importlib.util, os
from pathlib import Path
from unittest.mock import patch
from plangraph.spawner.host_ready import cargo_lease, shared_cargo_target
spec = importlib.util.spec_from_file_location('preflight', 'tools/preflight.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
seen = []
def run(command, cwd, env):
    assert env['CARGO_TARGET_DIR'] == str(shared_cargo_target())
    assert env['CARGO_INCREMENTAL'] == '0'
    if command[0] == 'cargo':
        try:
            with cargo_lease(exclusive=True):
                raise AssertionError('preflight did not lease shared target')
        except OSError:
            pass
        seen.append(command)
    return 0, ''
with patch.object(p, 'run', run), patch.object(p, 'rust_commands', return_value=[['cargo', 'test']]):
    assert p.main(['--base', 'HEAD', '--fast']) == 0
# The policy step's one pg-policy build runs first, under the same shared lease.
assert [c[:2] for c in seen] == [['cargo', 'build'], ['cargo', 'test']], seen
assert not Path(os.environ['CARGO_TARGET_DIR']).exists()
seen.clear()
with patch.dict(os.environ, PLANGRAPH_CARGO_TARGET_CAP_GB='0'), patch.object(p, 'run', run), patch.object(p, 'rust_commands', return_value=[['cargo', 'test']]):
    assert p.main(['--base', 'HEAD', '--fast']) == 1
assert not seen, 'over-cap preflight built anyway'
"#])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
