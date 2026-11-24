use super::super::{replay, target::jail_binary, Result, Step};
use super::{
    cargo_inputs::CargoInputs,
    receipt::{Observation, Outcome, TestResult},
    rust_source::{self, Name},
};
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
};

pub(super) type Changed = BTreeMap<String, Vec<Name>>;

fn regular(root: &Path, relative: &str) -> Result<PathBuf> {
    let path = root.join(relative);
    let mut entry = path.as_path();
    while entry != root {
        if entry.is_symlink() {
            return Err(format!("Rust test replay refuses a symlink: {relative}"));
        }
        entry = entry.parent().ok_or("Rust source outside checkout")?;
    }
    Ok(path)
}

fn source(path: &Path) -> Result<String> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(format!("{}: {error}", path.display())),
    }
}

pub(super) fn changed(base: &Path, head: &Path, paths: &[String]) -> Result<Changed> {
    let mut result = Changed::new();
    for path in paths {
        let before = source(&regular(base, path)?)?;
        let after = source(&regular(head, path)?)?;
        let tests = rust_source::changed(&before, &after)
            .map_err(|e| format!("Rust test selection inconclusive for {path}: {e}"))?;
        if !tests.is_empty() {
            result.insert(path.clone(), tests);
        }
    }
    Ok(result)
}

#[derive(Deserialize)]
struct Metadata {
    packages: Vec<Package>,
}

#[derive(Deserialize)]
struct Package {
    name: String,
    manifest_path: PathBuf,
    targets: Vec<Target>,
}

#[derive(Deserialize)]
struct Target {
    name: String,
    kind: Vec<String>,
    src_path: PathBuf,
    test: bool,
}

impl Target {
    fn kind(&self) -> Option<&'static str> {
        ["lib", "bin", "test", "example", "bench"]
            .into_iter()
            .find(|kind| self.kind.iter().any(|name| name == kind))
            .or_else(|| {
                self.kind
                    .iter()
                    .any(|name| {
                        matches!(
                            name.as_str(),
                            "proc-macro" | "rlib" | "dylib" | "cdylib" | "staticlib"
                        )
                    })
                    .then_some("lib")
            })
    }

    fn select(&self, cmd: &mut Command) -> Option<()> {
        let kind = self.kind()?;
        cmd.arg(format!("--{kind}"));
        if kind != "lib" {
            cmd.arg(&self.name);
        }
        Some(())
    }

    fn id(&self, package: &str, test: &str) -> String {
        let kind = self.kind().unwrap_or("unknown");
        if kind == "lib" {
            format!("{package}:lib::{test}")
        } else {
            format!("{package}:{kind}:{}::{test}", self.name)
        }
    }
}

fn metadata(
    root: &Path,
    manifest: &str,
    target: &Path,
    tool_path: Option<&str>,
) -> Result<Metadata> {
    let mut cmd = Command::new("cargo");
    if let Some(path) = tool_path {
        cmd.env("PATH", path);
    }
    replay::scrub_test_env(&mut cmd);
    let output = cmd
        .current_dir(root)
        .env("CARGO_TARGET_DIR", target)
        .args([
            "metadata",
            "--no-deps",
            "--format-version",
            "1",
            "--manifest-path",
            manifest,
        ])
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "Rust test target discovery inconclusive for {manifest}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    serde_json::from_slice(&output.stdout).map_err(|error| error.to_string())
}

fn arguments(step: &Step) -> Option<(&str, &str)> {
    if !step.label.ends_with("/test") {
        return None;
    }
    let args = step.args.rsplit(|arg| arg == "--").next()?;
    let value = |flag: &str| {
        args.iter()
            .position(|arg| arg == flag)
            .and_then(|index| args.get(index + 1))
            .map(String::as_str)
    };
    Some((value("--manifest-path")?, value("-p")?))
}

fn run_target(
    root: &Path,
    step: &Step,
    target: &Target,
    tests: &[String],
    cargo: &Path,
    tool_path: Option<&str>,
) -> Result<BTreeMap<String, Observation>> {
    if tests.is_empty() {
        return Ok(BTreeMap::new());
    }
    let mut cmd = Command::new(&step.args[0]);
    if let Some(path) = tool_path {
        cmd.env("PATH", path);
    }
    replay::scrub_test_env(&mut cmd);
    cmd.current_dir(root)
        .args(&step.args[1..])
        .arg("--color=never");
    target
        .select(&mut cmd)
        .ok_or("unsupported Cargo test target")?;
    cmd.args(["--", "--exact", "--format", "pretty"])
        .args(tests)
        .env("CARGO_TARGET_DIR", cargo)
        .env("CARGO_INCREMENTAL", "0")
        .env("PG_JAIL_BIN", jail_binary(cargo));
    if step.label == "pg-worker/test" {
        cmd.env("PG_JAIL", "off").env("PG_TEST_ALLOW_UNJAILED", "1");
    }
    let output = cmd.output().map_err(|error| error.to_string())?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let summary = stdout
        .lines()
        .find(|line| line.starts_with("test result: "));
    let valid = summary.is_some_and(|line| {
        (line.starts_with("test result: ok.") && output.status.success())
            || (line.starts_with("test result: FAILED.") && output.status.code() == Some(101))
    });
    Ok(tests
        .iter()
        .map(|test| {
            let prefix = format!("test {test} ... ");
            let mut body = stdout.lines().filter_map(|line| line.strip_prefix(&prefix));
            let observed = body.next().filter(|_| body.next().is_none());
            let result = match (valid, observed) {
                (true, Some("ok" | "FAILED")) => {
                    let state = observed.unwrap_or_default();
                    let outcome = if state == "ok" {
                        Outcome::Passed
                    } else {
                        Outcome::Failed
                    };
                    Observation {
                        outcome,
                        inactive: false,
                        summary: format!("{prefix}{state}; {}", summary.unwrap_or_default()),
                    }
                }
                _ => Observation::inconclusive(
                    summary.unwrap_or("Cargo build, fixture or runner error"),
                ),
            };
            (test.clone(), result)
        })
        .collect())
}

struct Workspace<'a> {
    base: &'a Path,
    head: &'a Path,
}

impl Workspace<'_> {
    fn overlay(
        &self,
        path: &str,
        names: &[Name],
        integration_root: bool,
    ) -> Result<BTreeSet<Name>> {
        let base_file = regular(self.base, path)?;
        let head_file = regular(self.head, path)?;
        if !base_file.is_file() {
            if !integration_root {
                return Ok(names.iter().cloned().collect());
            }
            fs::create_dir_all(base_file.parent().ok_or("missing Rust test directory")?)
                .and_then(|_| fs::copy(head_file, base_file))
                .map_err(|e| e.to_string())?;
            return Ok(BTreeSet::new());
        }
        let (overlay, unavailable) =
            rust_source::overlay(&source(&base_file)?, &source(&head_file)?, names)
                .map_err(|e| format!("Rust test overlay inconclusive for {path}: {e}"))?;
        fs::write(base_file, overlay).map_err(|e| e.to_string())?;
        Ok(unavailable.into_iter().collect())
    }
}

pub(super) fn run(
    base: &Path,
    head: &Path,
    changed: &Changed,
    steps: &[Step],
    cargo: (&Path, &CargoInputs),
    tool_path: Option<&str>,
) -> Result<Vec<TestResult>> {
    let (cargo, inputs) = cargo;
    let workspace = Workspace { base, head };
    let mut manifests = BTreeMap::new();
    for (manifest, _) in steps.iter().filter_map(arguments) {
        if !manifests.contains_key(manifest) {
            manifests.insert(manifest, metadata(head, manifest, cargo, tool_path)?);
        }
    }
    let mut rows = Vec::new();
    for (path, tests) in changed {
        let mut targets = Vec::new();
        for step in steps {
            let Some((manifest, name)) = arguments(step) else {
                continue;
            };
            let Some(package) = manifests[manifest].packages.iter().find(|p| p.name == name) else {
                continue;
            };
            let file = head.join(path);
            if !package
                .manifest_path
                .parent()
                .is_some_and(|dir| file.starts_with(dir))
            {
                continue;
            }
            for target in package
                .targets
                .iter()
                .filter(|t| t.test && t.kind().is_some())
            {
                for module in rust_source::module_files(head, &target.src_path, &file)? {
                    targets.push((step, package, target, module));
                }
            }
        }
        if targets.is_empty() {
            return Err(format!(
                "Rust test target inconclusive for {path}: no target for {}",
                tests
                    .iter()
                    .map(|t| t.qualified(&[]))
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        let integration = targets
            .iter()
            .any(|(_, _, target, module)| target.kind() == Some("test") && module.is_empty());
        let base_file = base.join(path);
        let original = base_file
            .is_file()
            .then(|| fs::read(&base_file))
            .transpose()
            .map_err(|error| error.to_string())?;
        let missing = workspace.overlay(path, tests, integration)?;
        for (step, package, target, module) in targets {
            let ids = tests
                .iter()
                .map(|name| name.qualified(&module))
                .collect::<Vec<_>>();
            let available = tests
                .iter()
                .zip(&ids)
                .filter(|(name, _)| !missing.contains(*name))
                .map(|(_, id)| id.clone())
                .collect::<Vec<_>>();
            inputs.freshen(base)?;
            let mut on_base = run_target(base, step, target, &available, cargo, tool_path)?;
            inputs.freshen(head)?;
            let mut on_head = run_target(head, step, target, &ids, cargo, tool_path)?;
            for id in ids {
                let head = on_head.remove(&id).ok_or("missing head test result")?;
                let base = on_base.remove(&id).unwrap_or_else(|| {
                    Observation::inconclusive("test source or production module absent on base")
                });
                rows.push(TestResult {
                    id: target.id(&package.name, &id),
                    path: path.clone(),
                    backend: "cargo".into(),
                    base,
                    head,
                    exception: None,
                });
            }
        }
        if let Some(original) = original {
            fs::write(base_file, original).map_err(|e| e.to_string())?;
        } else if base_file.is_file() {
            fs::remove_file(base_file).map_err(|e| e.to_string())?;
        }
    }
    Ok(rows)
}
