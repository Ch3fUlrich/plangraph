//! Caller-process launch for the fleet's session command.

mod arguments;
mod cargo_env;
mod external_run;
mod identity;
mod paths;
mod placement;
mod policy;
mod process;
mod remote;
mod request;
mod restart;
pub mod round_cli;
#[cfg(test)]
mod round_cli_tests;
#[cfg(test)]
mod round_episode_tests;
mod round_host;
#[cfg(test)]
mod round_host_tests;
pub mod round_native;
#[cfg(test)]
mod round_native_tests;
mod round_open;
pub mod round_reader;
pub mod round_signal;
pub mod round_state;
pub mod round_store;
pub mod round_time;
#[cfg(test)]
mod round_time_tests;
pub mod round_types;
pub mod round_wait;
pub mod round_watch;
#[cfg(test)]
mod round_watch_crash_tests;
#[cfg(test)]
mod round_watch_tests;
mod settings;
mod storage;
#[cfg(test)]
mod tests;

pub(crate) use external_run::agent_roster;
use paths::{io_text, normalize, read_text, resolve, shell_quote, var, Paths};
use policy::Admission;
use regex::Regex;
use request::{Request, ResumeIdentity};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    borrow::Cow,
    io::{self, BufWriter, IsTerminal, Write},
    path::Path,
    process::{Command, Stdio},
    sync::LazyLock,
};

/// Read a watchdog attendance lease at Unix time `now`, deleting future-dated leases.
pub fn is_attended(marker: &Path, window: u64, now: f64) -> bool {
    policy::is_attended(marker, window, now)
}

/// Select a host with the legacy helper's local fallback and diagnostics.
pub fn legacy_host(policy_path: &Path, explicit: Option<&str>) -> Result<String, String> {
    placement::legacy_host(policy_path, explicit)
}

/// Local kernel hostname used when the spawner host identity is unset.
pub fn local_hostname() -> String {
    placement::fs_hostname()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
struct Caps {
    handoff: u64,
    hard: u64,
}

impl Caps {
    fn new(handoff: u64, hard: u64) -> Option<Self> {
        (handoff > 0 && hard > handoff).then_some(Self { handoff, hard })
    }

    fn from_value(value: &Value) -> Option<Self> {
        Self::new(
            value.get("handoff")?.as_u64()?,
            value.get("hard")?.as_u64()?,
        )
    }
}

fn role_of(name: &str) -> &str {
    static SEQ: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"-\d{4,}$").unwrap());
    SEQ.find(name.strip_suffix('\n').unwrap_or(name))
        .map_or(name, |m| &name[..m.start()])
}

/// Run one session launch with fleet policy and settings from the supplied ops directory.
pub fn run(args: Vec<String>, ops: &Path) -> i32 {
    let stdout = io::stdout().lock();
    // A caller with a pipe observes the same output on interruption as it does
    // with the Python launcher, including when no final started line was sent.
    let mut out: Box<dyn Write> = if stdout.is_terminal() || var("PYTHONUNBUFFERED").is_some() {
        Box::new(stdout)
    } else {
        Box::new(BufWriter::new(stdout))
    };
    let result = request::launch(args, &Paths::new(ops), &mut out);
    let flushed = out.flush();
    match result.and_then(|code| flushed.map(|()| code)) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("fleet-launch: {error}");
            1
        }
    }
}

fn local(
    request: &Request,
    paths: &Paths,
    admission: &Admission,
    host: &str,
    resume: Option<&ResumeIdentity>,
    out: &mut dyn Write,
) -> io::Result<i32> {
    let args = &request.args;
    let message = admission.message(request.legacy_respawn);
    let admission_suffix = if message.is_empty() {
        String::new()
    } else {
        format!(": {message}")
    };
    if !args.dry {
        if let Some(error) = admission.caps.error {
            eprintln!("fleet-launch: warning: {error}");
        }
        if !message.is_empty() {
            eprintln!("fleet-launch: admission{admission_suffix}");
        }
    }
    let log = |outcome, name| {
        if args.dry {
            return true;
        }
        storage::record_outcome(admission, outcome, name)
    };
    if !admission.allowed {
        if args.dry {
            eprintln!("fleet-launch: admission refused{admission_suffix}");
        }
        log("refused", None);
        return Ok(2);
    }
    if !Path::new(&args.cwd).is_dir() {
        log("refused", None);
        eprintln!("fleet-launch: cwd {} is not a directory", args.cwd);
        return Ok(2);
    }
    let prompt_path = Path::new(&args.prompt);
    let supplied = || read_writer_prompt(prompt_path);
    let prompt = match request
        .restart
        .as_ref()
        .map_or_else(supplied, |restart| Ok(restart.prompt.clone()))
    {
        Ok(prompt) => prompt,
        Err(error) => {
            log("refused", None);
            eprintln!(
                "fleet-launch: prompt file: {}",
                io_text(&error, prompt_path)
            );
            return Ok(2);
        }
    };
    let mcp = args
        .option("mcp")
        .filter(|s| !s.is_empty())
        .map_or_else(|| paths.mcp(), Into::into);
    let mcp = resolve(&mcp);
    if !mcp.is_file() {
        log("refused", None);
        eprintln!("fleet-launch: mcp config {} not found", mcp.display());
        return Ok(2);
    }
    let style = read_text(&paths.ops.join("terse-style.txt")).unwrap_or_default();
    let style = style.trim_end_matches('\n');
    let routing_style;
    let style = if args.role == "routing" {
        routing_style = format!(
            "{style}\n\nInvoke the skill router-core at startup (skills/router-core/SKILL.md in the plangraph checkout). Follow its Batch mode procedure for multi-item router work."
        );
        &routing_style
    } else {
        style
    };
    let _lock = if resume.is_none()
        && !args.dry
        && var("FLEET_DEFER_RECORD").as_deref() != Some("1")
    {
        match storage::role_lock(&paths.state, &args.role) {
            Ok(file) => Some(file),
            Err(error) => {
                log("refused", None);
                eprintln!("fleet-launch: {error}; no launch (a {} session may be live or starting: check `claude agents --json`)", args.role);
                return Ok(2);
            }
        }
    } else {
        None
    };
    let seq = if let Some(resume) = resume {
        resume.seq
    } else if args.dry {
        0
    } else if let Some(given) = var("FLEET_SESSION_SEQ")
        .filter(|s| s.bytes().all(|b| b.is_ascii_digit()))
        .and_then(|s| s.parse::<u64>().ok())
        .filter(|n| *n > 0)
    {
        given
    } else {
        storage::next_seq(&paths.state)?
    };
    let name = resume.map_or_else(
        || Cow::Owned(format!("{}-{seq:04}", args.role)),
        |resume| Cow::Borrowed(resume.name.as_str()),
    );
    let hard = admission.caps.caps.hard;
    let settings = match settings::prepare(paths, &args.role, admission.caps.caps, !args.dry) {
        Ok(settings) => settings,
        Err(error) => {
            log("failed", None);
            eprintln!("fleet-launch: launch settings for hard cap {hard}: {error}");
            return Ok(2);
        }
    };
    let compact = &settings.compact;
    let deny = if role_of(&name) == "routing" {
        "Artifact,SendFeedback,Workflow,ReportFindings"
    } else {
        "Artifact,SendFeedback,Workflow,ReportFindings,AskUserQuestion"
    };
    let mut argv = vec!["claude", "--bg"];
    if let Some(resume) = resume {
        argv.extend(["--resume", resume.id.as_str()]);
    }
    let mcp_arg = mcp.to_string_lossy();
    let settings_arg = settings.path.to_string_lossy();
    argv.extend([
        "--strict-mcp-config",
        "--mcp-config",
        &mcp_arg,
        "--remote-control",
        &name,
        "-n",
        &name,
        "--model",
        &args.model,
        "--effort",
        &args.effort,
        "--permission-mode",
        "bypassPermissions",
        "--settings",
        &settings_arg,
        "--disallowed-tools",
        deny,
        "--append-system-prompt",
        style,
        &prompt,
    ]);
    if args.dry {
        eprintln!("fleet-launch: admission allowed{admission_suffix}");
        if settings.generated {
            eprintln!("fleet-launch: dry run: {} is written by the real launch (lean profile + both compact variables = {compact})", settings.path.display());
        }
        let command = argv
            .iter()
            .map(|arg| {
                if *arg == style || *arg == prompt {
                    format!("<{} chars>", arg.chars().count())
                } else {
                    shell_quote(arg)
                }
            })
            .collect::<Vec<_>>()
            .join(" ");
        writeln!(
            out,
            "cd {} && CLAUDE_CODE_AUTO_COMPACT_WINDOW={compact} {command}",
            shell_quote(&args.cwd)
        )?;
        return Ok(0);
    }
    // Resolved before the durable "starting" row and the registration: a bad setting must fail the launch cleanly.
    let target_env = match cargo_env::target_env(
        std::env::var_os("CARGO_TARGET_DIR").as_deref(),
        std::env::var_os("PLANGRAPH_CARGO_TARGET_DIR").as_deref(),
        &paths.home,
        &std::env::current_dir().unwrap_or_default(),
    ) {
        Ok(env) => env,
        Err(error) => {
            log("refused", None);
            eprintln!("fleet-launch: {error}");
            return Ok(2);
        }
    };
    if storage::ledger(admission, true, "starting", Some(&name)).is_err() {
        eprintln!("fleet-launch: fallback ledger unavailable; not starting");
        return Ok(2);
    }
    let mut lifecycle = external_run::register(args, admission, &name, host)?;
    let launched_ms = storage::epoch_millis();
    let output = Command::new(argv[0])
        .args(&argv[1..])
        .stdin(Stdio::inherit())
        .current_dir(&args.cwd)
        .envs(target_env)
        .env("CLAUDE_CODE_AUTO_COMPACT_WINDOW", compact)
        .env("FLEET_SESSION_NAME", &args.role)
        .env("PATH", settings::tool_path(&paths.home))
        .env_remove("FLEET_FORWARDED")
        .env_remove("FLEET_DEFER_RECORD")
        .output();
    let output = match output {
        Ok(output) => output,
        Err(_) => {
            lifecycle.finish("failed", 127, Some("launch_unavailable"))?;
            log("failed", Some(&name));
            eprintln!("fleet-launch: launch executable unavailable");
            return Ok(2);
        }
    };
    let stdout = normalize(&String::from_utf8_lossy(&output.stdout));
    write!(out, "{stdout}")?;
    io::stderr().write_all(normalize(&String::from_utf8_lossy(&output.stderr)).as_bytes())?;
    let outcome = if output.status.success() {
        "started"
    } else {
        "failed"
    };
    if !log(outcome, Some(&name))
        && output.status.success()
        && storage::ledger(admission, true, "started", Some(&name)).is_err()
    {
        eprintln!("fleet-launch: durable starting row retained; session may be live; do not retry");
    }
    if !output.status.success() {
        let code = remote::exit_code(&output);
        lifecycle.finish("failed", code, Some("launch_failed"))?;
        return Ok(code);
    }
    writeln!(out, "fleet-launch: session {name}")?;
    let sid = identity::session_id(&stdout, &name, launched_ms, &args.cwd)
        .or_else(|| resume.map(|resume| resume.id.clone()));
    lifecycle.save("session_id", json!(sid))?;
    if sid.is_none() {
        eprintln!("fleet-launch: warning: no session id for {name}; fleet hooks will not find this session");
    }
    let spec = storage::publish(
        request,
        paths,
        admission,
        &mcp,
        &name,
        seq,
        (&lifecycle, sid.as_deref()),
    )?;
    if let Some(sid) = sid {
        storage::setup(paths, &spec, admission.caps.caps, &sid, &name)?;
        writeln!(out, "fleet-launch: started {sid}")?;
    } else {
        if spec["oom_protect"] == true || spec["ctx_guard"].is_object() {
            eprintln!("fleet-launch: warning: no session id for {name}; oom_protect/ctx_guard not applied");
        }
        writeln!(out, "fleet-launch: started-unknown {name}")?;
    }
    Ok(0)
}

fn read_writer_prompt(path: &Path) -> io::Result<String> {
    let text = read_text(path)?;
    match crate::batch_writer_brief::render(&text).map_err(io::Error::other)? {
        Cow::Borrowed(_) => Ok(text),
        Cow::Owned(brief) => {
            const SKILL: &str = include_str!("../../../../skills/batch-writer/SKILL.md");
            Ok(crate::role_header::compose("claude", &brief, Some(SKILL)))
        }
    }
}
