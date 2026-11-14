//! Exercise the actual Python entry points from Rust regression tests.
use std::{path::PathBuf, process::Command};

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
fn python(source: &str) {
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .args(["-c", source])
        .current_dir(root())
        .output()
        .unwrap();
    assert!(output.status.success(), "{}{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
}

#[test]
fn prepush_refuses_invalid_trailers_before_scrub_or_preflight() {
    python(r#"
import io, os, pathlib, runpy, subprocess, tempfile
from unittest.mock import patch
hook = runpy.run_path('tools/hooks/pre-push')
f = hook['push_main']
g = f.__globals__
with tempfile.TemporaryDirectory() as tmp:
    def git(*args):
        return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.PIPE).decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-qm', 'baseline')
    base = git('rev-parse', 'HEAD')
    git('update-ref', 'refs/remotes/origin/main', base)
    path = pathlib.Path(tmp, '.github/workflows/tests.yml')
    path.parent.mkdir(parents=True)
    for trailer, expected in [('Hand-Edit: fixture', 'hand-edit touches auth/door'), ('Writer: codex/gpt-6.1-sol-medium', 'gate edit without Gate-Edit')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trailer)
        git('add', '.')
        git('commit', '-qm', 'fixture\n\nPlan-Item: plangraph:TEST\n' + trailer)
        head = git('rev-parse', 'HEAD')
        old = os.getcwd()
        os.chdir(tmp)
        try:
            err = io.StringIO()
            with patch.dict(g, file_size_gate=lambda: 0), patch.object(g['sc'], 'build_config', side_effect=AssertionError('scrub started before trailer refusal')), patch('sys.stdin', io.StringIO(f'refs/heads/topic {head} refs/heads/topic {base}\n')), patch('sys.stderr', err):
                assert f() != 0
            assert expected in err.getvalue(), err.getvalue()
        finally:
            os.chdir(old)
        git('reset', '--hard', base)
"#);
}
