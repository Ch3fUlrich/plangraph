//! CI regressions execute the workflow's real bounded runner.
use saphyr::{LoadableYamlNode, Yaml};
use std::{path::Path, process::Command};

fn python(source: &str) {
    let output = Command::new(if cfg!(windows) { "python" } else { "python3" })
        .args(["-c", source])
        .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn text<'a>(node: Option<&'a Yaml<'a>>) -> &'a str {
    node.and_then(Yaml::as_str).unwrap_or("")
}

fn minutes(node: Option<&Yaml>) -> Option<i64> {
    node.and_then(Yaml::as_integer)
}

#[test]
fn every_pytest_step_has_a_timeout_below_its_job() {
    python(
        r#"
import pathlib, yaml
workflow = yaml.safe_load(pathlib.Path('.github/workflows/tests.yml').read_text())
seen = 0
for name, job in workflow['jobs'].items():
    for step in job.get('steps', []):
        script = step.get('run', '') + step.get('with', {}).get('command', '')
        if name.startswith('pytest') or 'pytest' in script:
            seen += 1
            limit = step.get('timeout-minutes', 0)
            assert 0 < limit < job.get('timeout-minutes', 360), (name, step)
            if 'pytest' in script:
                assert step.get('uses') == './.github/actions/bounded-step', step
            if step.get('uses') == './.github/actions/bounded-step':
                assert 0 < int(step['with']['seconds']) + 1 < limit * 60, step
assert seen >= 20, seen
"#,
    );
}

#[cfg(unix)]
#[test]
fn bounded_step_keeps_logs_and_exit_status_without_inheriting_a_pipe() {
    python(
        r#"
import os, pathlib, signal, subprocess, tempfile, time, yaml
action = yaml.safe_load(pathlib.Path('.github/actions/bounded-step/action.yml').read_text())
script = action['runs']['steps'][0]['run']
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, RUNNER_TEMP=tmp, CI_STEP_SECONDS='0.2s')
    def run(command):
        return subprocess.run(['bash', '-euo', 'pipefail', '-c', script],
            env=dict(env, CI_STEP_COMMAND=command), capture_output=True, text=True, timeout=5)
    for command, code in [('echo success', 0), ('echo failure >&2; exit 17', 17)]:
        result = run(command)
        assert result.returncode == code, result
        assert command.split()[1] in result.stdout, result
    result = run('echo refusing-term; trap \"\" TERM; while :; do :; done')
    assert result.returncode == 137, result
    assert 'refusing-term' in result.stdout, result
    # A detached descendant retains stdout but must not hold the runner's pipe.
    try:
        started = time.monotonic()
        result = run("echo before-hang; setsid bash -c 'echo $$ > \"$RUNNER_TEMP/escaped.pid\"; trap \"\" TERM; while :; do :; done' & wait")
        assert result.returncode in (124, 137), result
        assert time.monotonic() - started < 5
        assert 'before-hang' in result.stdout, result
        logs = list(pathlib.Path(tmp).glob('ci-step.*.log'))
        assert any('before-hang' in log.read_text() for log in logs), logs
    finally:
        pidfile = pathlib.Path(tmp, 'escaped.pid')
        if pidfile.exists():
            os.killpg(int(pidfile.read_text()), signal.SIGKILL)
"#,
    );
}
