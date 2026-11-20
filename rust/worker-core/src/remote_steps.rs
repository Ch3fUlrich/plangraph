//! A shell-free step runner: one JSON plan, steps run as argv, one exit code.
//!
//! `pg-spawnd steps` reads a [`Plan`] on stdin. The WSL hop carries it because
//! `wsl.exe --exec` runs no shell and the steps need ordering, an environment file,
//! file writes, a commit-presence fallback and a bracketed receipt.
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    fs,
    io::{self, Write},
    path::Path,
    process::{Command, Stdio},
};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    /// KEY=VALUE lines (optional `export`, quotes, `#` comments) applied to every step.
    #[serde(default)]
    pub env_file: Option<String>,
    #[serde(default)]
    pub cwd: Option<String>,
    /// Run every step and keep the first failing code; the default stops at the first failure.
    #[serde(default)]
    pub keep_going: bool,
    pub steps: Vec<Step>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    #[serde(default)]
    pub argv: Option<Vec<String>>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Text fed to the step's stdin, or the content of a `write_file`.
    #[serde(default)]
    pub stdin_text: Option<String>,
    /// Binary content as hex, for `write_file`.
    #[serde(default)]
    pub stdin_hex: Option<String>,
    #[serde(default)]
    pub write_file: Option<WriteFile>,
    #[serde(default)]
    pub ensure_commit: Option<EnsureCommit>,
    #[serde(default)]
    pub receipt: Option<Receipt>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WriteFile {
    pub path: String,
    pub mode: u32,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnsureCommit {
    pub sha: String,
    pub fetch_argv: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Receipt {
    pub begin: String,
    pub end: String,
}

/// The `KEY=VALUE` pairs of an environment file; a line that is not one is an error.
pub fn parse_env_file(text: &str) -> Result<Vec<(String, String)>, String> {
    let mut pairs = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line).trim_start();
        let (key, value) = line.split_once('=').ok_or("env file line without =")?;
        if key.is_empty()
            || key.as_bytes()[0].is_ascii_digit()
            || !key.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
        {
            return Err("env file key invalid".into());
        }
        let value = value.trim();
        let value = match value.as_bytes() {
            [q @ (b'"' | b'\''), .., last] if q == last && value.len() >= 2 => {
                &value[1..value.len() - 1]
            }
            _ => value,
        };
        pairs.push((key.to_owned(), value.to_owned()));
    }
    Ok(pairs)
}

fn decode_hex(text: &str) -> Result<Vec<u8>, String> {
    if text.len() % 2 != 0 {
        return Err("hex content has odd length".into());
    }
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).map_err(|e| e.to_string()))
        .collect()
}

fn command(
    argv: &[String],
    env: &[(String, String)],
    cwd: Option<&Path>,
) -> Result<Command, String> {
    let (program, args) = argv.split_first().ok_or("empty argv")?;
    let mut cmd = Command::new(program);
    cmd.args(args).envs(env.iter().map(|(k, v)| (k, v)));
    if let Some(cwd) = cwd {
        cmd.current_dir(cwd);
    }
    Ok(cmd)
}

/// Run one argv; its stdout is copied to `out` so receipts and output stay in order.
fn run_argv(
    argv: &[String],
    env: &[(String, String)],
    cwd: Option<&Path>,
    stdin: Option<&[u8]>,
    out: &mut dyn Write,
) -> Result<i32, String> {
    let mut cmd = command(argv, env, cwd)?;
    cmd.stdin(if stdin.is_some() {
        Stdio::piped()
    } else {
        Stdio::null()
    })
    .stdout(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| e.to_string())?;
    let writer = stdin.map(|bytes| (child.stdin.take(), bytes.to_vec()));
    let mut stdout = child.stdout.take().ok_or("no stdout")?;
    let feeder = writer.and_then(|(pipe, bytes)| {
        pipe.map(|mut pipe| std::thread::spawn(move || pipe.write_all(&bytes)))
    });
    let copied = io::copy(&mut stdout, out);
    if let Some(feeder) = feeder {
        let _ = feeder.join();
    }
    out.flush().map_err(|e| e.to_string())?;
    copied.map_err(|e| e.to_string())?;
    let status = child.wait().map_err(|e| e.to_string())?;
    Ok(status.code().unwrap_or(1))
}

#[cfg(not(unix))]
fn write_file(_: &WriteFile, _: &[u8]) -> Result<(), String> {
    Err("write_file needs a Unix host".into())
}

#[cfg(unix)]
fn write_file(spec: &WriteFile, content: &[u8]) -> Result<(), String> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(spec.mode)
        .open(&spec.path)
        .map_err(|e| e.to_string())?;
    // The creation mode is masked by the process umask; the plan's mode is exact.
    file.set_permissions(fs::Permissions::from_mode(spec.mode))
        .map_err(|e| e.to_string())?;
    file.write_all(content).map_err(|e| e.to_string())
}

fn run_step(
    step: &Step,
    env: &[(String, String)],
    cwd: Option<&Path>,
    out: &mut dyn Write,
) -> Result<i32, String> {
    let mut env = env.to_vec();
    env.extend(step.env.iter().map(|(k, v)| (k.clone(), v.clone())));
    let content = match (&step.stdin_text, &step.stdin_hex) {
        (_, Some(hex)) => Some(decode_hex(hex)?),
        (Some(text), None) => Some(text.clone().into_bytes()),
        (None, None) => None,
    };
    if let Some(spec) = &step.write_file {
        write_file(spec, content.as_deref().unwrap_or_default())?;
        return Ok(0);
    }
    if let Some(ensure) = &step.ensure_commit {
        let present = [
            "git",
            "cat-file",
            "-e",
            &format!("{}^{{commit}}", ensure.sha),
        ]
        .map(String::from);
        if run_argv(&present, &env, cwd, None, out)? == 0 {
            return Ok(0);
        }
        let fetched = run_argv(&ensure.fetch_argv, &env, cwd, None, out)?;
        return Ok(if fetched == 0 {
            run_argv(&present, &env, cwd, None, out)?
        } else {
            fetched
        });
    }
    let argv = step.argv.as_deref().ok_or("step has no action")?;
    if let Some(receipt) = &step.receipt {
        writeln!(out, "{}", receipt.begin).map_err(|e| e.to_string())?;
        out.flush().map_err(|e| e.to_string())?;
        let code = run_argv(argv, &env, cwd, content.as_deref(), out);
        writeln!(out, "\n{}", receipt.end).map_err(|e| e.to_string())?;
        out.flush().map_err(|e| e.to_string())?;
        return code;
    }
    run_argv(argv, &env, cwd, content.as_deref(), out)
}

/// Run the plan; the result is the exit code (the first failing step's, else 0).
/// A step that cannot start or a bad env file counts as code 1 and is reported on stderr.
pub fn run(plan: &Plan, out: &mut dyn Write) -> i32 {
    let env = match &plan.env_file {
        Some(path) => match fs::read_to_string(path)
            .map_err(|e| e.to_string())
            .and_then(|text| parse_env_file(&text))
        {
            Ok(pairs) => pairs,
            Err(error) => {
                eprintln!("env file: {error}");
                return 1;
            }
        },
        None => Vec::new(),
    };
    let cwd = plan.cwd.as_deref().map(Path::new);
    if let Some(dir) = cwd {
        if !dir.is_dir() {
            eprintln!("cwd is not a directory");
            return 1;
        }
    }
    let mut first_failure = 0;
    for step in &plan.steps {
        let code = run_step(step, &env, cwd, out).unwrap_or_else(|error| {
            eprintln!("step failed: {error}");
            1
        });
        if code != 0 {
            if first_failure == 0 {
                first_failure = code;
            }
            if !plan.keep_going {
                break;
            }
        }
    }
    first_failure
}

/// Parse a plan from JSON text.
pub fn parse(text: &str) -> Result<Plan, String> {
    serde_json::from_str(text).map_err(|e| e.to_string())
}

/// Read the plan from `input`, run it into `out` and return its exit code.
pub fn execute(mut input: impl io::Read, out: &mut dyn Write) -> Result<i32, String> {
    let mut text = String::new();
    input.read_to_string(&mut text).map_err(|e| e.to_string())?;
    Ok(run(&parse(&text)?, out))
}
