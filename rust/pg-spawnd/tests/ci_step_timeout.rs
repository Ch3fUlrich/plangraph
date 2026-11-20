//! CI regressions execute the workflow's real bounded runner.
use saphyr::{LoadableYamlNode, Yaml};
use std::path::Path;
#[cfg(unix)]
use std::process::Command;

#[cfg(unix)]
fn python(source: &str, script: &str) {
    let output = Command::new("python3")
        .args(["-c", source])
        .env("BOUNDED_STEP_SCRIPT", script)
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
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/workflows/tests.yml");
    let source = std::fs::read_to_string(path).unwrap();
    let docs = Yaml::load_from_str(&source).unwrap();
    let jobs = docs[0]
        .as_mapping_get("jobs")
        .unwrap()
        .as_mapping()
        .unwrap();
    let mut seen = 0;
    for (name, job) in jobs {
        let name = name.as_str().unwrap();
        let job_limit = minutes(job.as_mapping_get("timeout-minutes")).unwrap_or(360);
        let steps = job.as_mapping_get("steps").and_then(Yaml::as_sequence);
        for step in steps.into_iter().flatten() {
            let with = step.as_mapping_get("with");
            let script = format!(
                "{}{}",
                text(step.as_mapping_get("run")),
                text(with.and_then(|with| with.as_mapping_get("command")))
            );
            if !(name.starts_with("pytest") || script.contains("pytest")) {
                continue;
            }
            seen += 1;
            let limit = minutes(step.as_mapping_get("timeout-minutes")).unwrap_or(0);
            assert!(0 < limit && limit < job_limit, "{name}: {step:?}");
            let bounded = text(step.as_mapping_get("uses")) == "./.github/actions/bounded-step";
            if script.contains("pytest") {
                assert!(bounded, "{name}: {step:?}");
            }
            if bounded {
                let seconds = with.and_then(|with| with.as_mapping_get("seconds"));
                let seconds = seconds
                    .and_then(Yaml::as_integer)
                    .or_else(|| text(seconds).parse().ok())
                    .unwrap();
                assert!(
                    0 < seconds + 1 && seconds + 1 < limit * 60,
                    "{name}: {step:?}"
                );
            }
        }
    }
    assert!(seen >= 20, "{seen}");
}

#[cfg(unix)]
#[test]
fn bounded_step_keeps_logs_and_exit_status_without_inheriting_a_pipe() {
    // The action's run script is read here, so the Python body needs no YAML module.
    let path =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../.github/actions/bounded-step/action.yml");
    let source = std::fs::read_to_string(path).unwrap();
    let docs = Yaml::load_from_str(&source).unwrap();
    let step = &docs[0]
        .as_mapping_get("runs")
        .and_then(|runs| runs.as_mapping_get("steps"))
        .and_then(Yaml::as_sequence)
        .unwrap()[0];
    let script = text(step.as_mapping_get("run"));
    assert!(!script.is_empty(), "bounded-step action has no run script");
    python(
        r#"
import os, pathlib, signal, subprocess, tempfile, time
script = os.environ['BOUNDED_STEP_SCRIPT']
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
        script,
    );
}
