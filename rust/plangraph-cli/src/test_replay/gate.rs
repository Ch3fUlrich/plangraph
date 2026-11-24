//! Replay changed tests against immutable commits with a test-only base overlay.
use super::{replay, selection::plan, target::resolve_cargo_target, Result};
use std::{collections::BTreeMap, fs, path::Path, process::Command};

mod cargo_inputs;
mod receipt;
mod rust_runner;
mod rust_source;
use cargo_inputs::CargoInputs;
pub use receipt::ReplayReceipt;
use receipt::{Outcome, TestResult};

fn python_test(path: &str) -> bool {
    Path::new(path)
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| {
            name.ends_with(".py") && (name.starts_with("test_") || name.ends_with("_test.py"))
        })
}

fn python(
    base: &Path,
    head: &Path,
    changed: &[String],
    cargo: Option<(&Path, &CargoInputs)>,
    tool_path: Option<&str>,
) -> Result<Vec<TestResult>> {
    if changed.is_empty() {
        return Ok(Vec::new());
    }
    let dir = tempfile::tempdir().map_err(|error| error.to_string())?;
    let runner = dir.path().join("pgtest.py");
    let record = dir.path().join("result.json");
    fs::write(&runner, include_str!("../../../../tools/pgtest.py"))
        .map_err(|error| error.to_string())?;
    let command = |root: &Path| {
        let mut command = Command::new(if cfg!(windows) { "python" } else { "python3" });
        replay::scrub_test_env(&mut command);
        command.current_dir(root).arg(&runner);
        if let Some(path) = tool_path {
            command.env("PATH", path);
        }
        command
    };
    let mut exact = command(head);
    if changed.iter().any(|p| p == "tests/test_fleet_snapshot.py") {
        let (target, inputs) = cargo.ok_or("missing fleet replay lease")?;
        for (side, root) in [("BASE", base), ("HEAD", head)] {
            let stage = dir.path().join(side);
            fs::create_dir(&stage).map_err(|e| e.to_string())?;
            inputs.freshen(root)?;
            let built = command(root)
                .arg("--replay-fleet")
                .arg(&stage)
                .env("CARGO_TARGET_DIR", target)
                .output()
                .map_err(|e| e.to_string())?;
            if built.status.success() {
                exact.env(
                    format!("PLANGRAPH_REPLAY_{side}_FLEET"),
                    stage.join(format!("pg-spawnd{}", std::env::consts::EXE_SUFFIX)),
                );
            }
        }
    }
    exact
        .arg("--exact-replay")
        .arg(base)
        .arg(head)
        .arg(&record)
        .args(changed);
    let output = exact.output().map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "pytest replay inconclusive for {}: {}",
            changed.join(", "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let bytes = fs::read(record).map_err(|error| error.to_string())?;
    serde_json::from_slice(&bytes).map_err(|error| error.to_string())
}

type Exceptions = BTreeMap<String, Vec<(String, String)>>;

fn exceptions(root: &Path, base: &str, head: &str) -> Result<Exceptions> {
    let text = replay::git(
        root,
        &[
            "log",
            "-z",
            "--format=%H%x00%(trailers:key=Test-Replay-Exception,valueonly,unfold)",
            &format!("{base}..{head}"),
        ],
    )?;
    let mut records = text.split_terminator('\0');
    let mut exceptions = Exceptions::new();
    while let (Some(commit), Some(trailers)) = (records.next(), records.next()) {
        for line in trailers
            .lines()
            .map(str::trim)
            .filter(|line| !line.is_empty())
        {
            let mut parsed = serde_json::Deserializer::from_str(line).into_iter::<String>();
            let invalid = || {
                format!("invalid Test-Replay-Exception in {commit}: expected a JSON test ID followed by a reason")
            };
            let id = parsed.next().ok_or_else(invalid)?.map_err(|_| invalid())?;
            let reason = &line[parsed.byte_offset()..];
            if !id.contains("::")
                || id.chars().any(char::is_control)
                || !reason.starts_with(' ')
                || reason.trim().is_empty()
                || reason.chars().any(char::is_control)
            {
                return Err(invalid());
            }
            exceptions
                .entry(id)
                .or_default()
                .push((commit.into(), reason.trim().into()));
        }
    }
    Ok(exceptions)
}

fn apply_exceptions(root: &Path, base: &str, head: &str, tests: &mut [TestResult]) -> Result<()> {
    let exceptions = exceptions(root, base, head)?;
    for test in tests {
        if test.base.outcome == Outcome::Failed {
            continue;
        }
        if let Some(claims) = exceptions.get(&test.id) {
            let revision = replay::git(root, &["rev-parse", &format!("{head}:{}", test.path)])?;
            for (commit, reason) in claims {
                if replay::git(root, &["rev-parse", &format!("{commit}:{}", test.path)]).as_ref()
                    == Ok(&revision)
                {
                    test.exception = Some(format!("{reason} (test file as of {commit})"));
                    break;
                }
            }
        }
    }
    Ok(())
}

/// Run the same immutable changed-test gate from standalone and native fast checks.
pub fn replay_committed(
    root: &Path,
    base: &str,
    head: &str,
    tool_path: Option<&str>,
) -> Result<ReplayReceipt> {
    let base = replay::resolve(root, base)?;
    let head = replay::resolve(root, head)?;
    let paths = replay::git(
        root,
        &[
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACMRT",
            "-z",
            &base,
            &head,
            "--",
        ],
    )?;
    let (py, rust): (Vec<_>, Vec<_>) = paths
        .split_terminator('\0')
        .filter(|path| python_test(path) || (path.starts_with("rust/") && path.ends_with(".rs")))
        .map(str::to_owned)
        .partition(|path| python_test(path));
    let claims = replay::verify(root, &base, &head)?;
    let mut tests = Vec::new();
    if !py.is_empty() || !rust.is_empty() {
        let checkout = replay::checkout(root, &head)?;
        let previous = replay::checkout(root, &base)?;
        let head_path = checkout
            .path()
            .canonicalize()
            .map_err(|error| error.to_string())?;
        let base_path = previous
            .path()
            .canonicalize()
            .map_err(|error| error.to_string())?;
        let rust = rust_runner::changed(&base_path, &head_path, &rust)?;
        let need_cargo = !rust.is_empty() || py.iter().any(|p| p == "tests/test_fleet_snapshot.py");
        let cargo = need_cargo
            .then(|| {
                let primary =
                    pg_worker_core::cargo_cache::shared_cache_roots().map_err(|e| e.to_string())?;
                resolve_cargo_target(root, &primary[0], "test-replay")
            })
            .transpose()?;
        let inputs = need_cargo
            .then(|| CargoInputs::prepare(&base_path, &head_path, &base, &head))
            .transpose()?;
        let cargo = cargo
            .as_ref()
            .zip(inputs.as_ref())
            .map(|(lease, inputs)| (lease.path.as_path(), inputs));
        tests.extend(python(&base_path, &head_path, &py, cargo, tool_path)?);
        if !rust.is_empty() {
            let changed = rust.keys().cloned().collect::<Vec<_>>();
            let (_, steps) = plan(&head_path, &base, &changed)?;
            let cargo = cargo.ok_or("missing Rust replay lease")?;
            tests.extend(rust_runner::run(
                &base_path, &head_path, &rust, &steps, cargo, tool_path,
            )?);
        }
    }
    apply_exceptions(root, &base, &head, &mut tests)?;
    ReplayReceipt::new(&base, &head, tests, claims)
}
