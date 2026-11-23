//! Caller-process launch for the fleet's session command.

mod arguments;
mod identity;
mod paths;
mod placement;
mod policy;
mod process;
mod remote;
mod request;
mod restart;
mod settings;
mod storage;
#[cfg(test)]
mod tests;

use arguments::{Arguments, ParseError};
use paths::{io_text, normalize, read_text, repr, resolve, shell_quote, var, Paths};
use placement::Placement;
use policy::{refused_role, Admission};
use regex::Regex;
use request::Request;
use serde::Serialize;
use serde_json::Value;
use std::{
    io::{self, BufWriter, IsTerminal, Write},
    path::{Path, PathBuf},
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
    let result = launch(args, &Paths::new(ops), &mut out);
    let flushed = out.flush();
    match result.and_then(|code| flushed.map(|()| code)) {
        Ok(code) => code,
        Err(error) => {
            eprintln!("fleet-launch: {error}");
            1
        }
    }
}

fn launch(args: Vec<String>, paths: &Paths, out: &mut dyn Write) -> io::Result<i32> {
    let forwarded = var("FLEET_FORWARDED").is_some();
    let args = match Arguments::parse(args) {
        Ok(args) => args,
        Err(ParseError::Usage) => {
            writeln!(out, "{}", arguments::USAGE)?;
            return Ok(2);
        }
        Err(ParseError::Guard(guard)) => {
            eprintln!(
                "fleet-launch: bad --ctx-guard {}: want N,M positive ints, hard > handoff",
                repr(&guard)
            );
            return Ok(2);
        }
    };
    let policy_path = paths.ops.join("fleet-launch-policy.json");
    let policy = policy::load(&policy_path);
    if let Some(pattern) = refused_role(policy.as_ref(), &args.role) {
        eprintln!("fleet-launch: role {} is refused by policy ({pattern}); no launch: dispatch the work as spawner runs from the L1", args.role);
        return Ok(2);
    }
    let Some(mut request) = Request::new(args, paths) else {
        return Ok(2);
    };
    match restart::prepare(&request, paths, policy.as_ref()) {
        Ok(Some(restart)) => {
            request.restart = Some(restart);
        }
        Ok(None) => {}
        Err(reason) => {
            if !request.args.dry {
                let admission = Admission::check(&request, policy.as_ref(), None, &paths.state);
                storage::record_outcome(&admission, "refused", None);
            }
            eprintln!("fleet-launch: PROMPT-STALE {reason}");
            return Ok(2);
        }
    }
    let host_policy = std::env::var_os("FLEET_HOST_POLICY").map_or(policy_path, PathBuf::from);
    let host_policy = policy::load(&host_policy);
    let placement = Placement::new(&request, host_policy.as_ref());
    let host = match placement.choose(&request, forwarded) {
        Ok(host) => host,
        Err(error) => {
            eprintln!("fleet-launch: {error}");
            return Ok(2);
        }
    };
    if !placement.local(&host) {
        return remote::launch(&request, paths, &host, out);
    }
    let admission = Admission::check(
        &request,
        policy.as_ref(),
        placement.policy_host(&host),
        &paths.state,
    );
    local(&request, paths, &admission, out)
}

fn local(
    request: &Request,
    paths: &Paths,
    admission: &Admission,
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
    let supplied = || read_text(prompt_path);
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
            "{style}\n\nRead skills/router-core/SKILL.md at startup. Follow its Batch mode procedure for multi-item router work."
        );
        &routing_style
    } else {
        style
    };
    let _lock = if !args.dry && var("FLEET_DEFER_RECORD").as_deref() != Some("1") {
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
    let seq = if args.dry {
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
    let name = format!("{}-{seq:04}", args.role);
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
    let argv = [
        "claude",
        "--bg",
        "--strict-mcp-config",
        "--mcp-config",
        &mcp.to_string_lossy(),
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
        &settings.path.to_string_lossy(),
        "--disallowed-tools",
        deny,
        "--append-system-prompt",
        style,
        &prompt,
    ];
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
    if storage::ledger(admission, true, "starting", Some(&name)).is_err() {
        eprintln!("fleet-launch: fallback ledger unavailable; not starting");
        return Ok(2);
    }
    let launched_ms = storage::epoch_millis();
    let output = Command::new(argv[0])
        .args(&argv[1..])
        .stdin(Stdio::inherit())
        .current_dir(&args.cwd)
        .env("CLAUDE_CODE_AUTO_COMPACT_WINDOW", compact)
        .env("FLEET_SESSION_NAME", &args.role)
        .env("PATH", settings::tool_path(&paths.home))
        .env_remove("FLEET_FORWARDED")
        .env_remove("FLEET_DEFER_RECORD")
        .output();
    let output = match output {
        Ok(output) => output,
        Err(_) => {
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
        return Ok(remote::exit_code(&output));
    }
    writeln!(out, "fleet-launch: session {name}")?;
    let sid = identity::session_id(&stdout, &name, launched_ms, &args.cwd);
    if sid.is_none() {
        eprintln!("fleet-launch: warning: no session id for {name}; fleet hooks will not find this session");
    }
    let spec = storage::publish(request, paths, admission, &mcp, &name, seq, sid.as_deref())?;
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
