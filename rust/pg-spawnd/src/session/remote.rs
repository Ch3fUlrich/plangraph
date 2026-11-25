use super::{
    paths::{basename, io_text, normalize, read_text, repr, resolve, shell_quote, Paths},
    placement::Host,
    process::capture,
    read_writer_prompt,
    storage::{next_seq, record_path, register_child},
    Caps, Request,
};
use crate::pyjson::preserve_order;
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{
    fs,
    io::{self, Write},
    os::unix::process::ExitStatusExt,
    path::Path,
    process::Output,
    time::Duration,
};

const LOADER: &str = "import base64,sys;exec(base64.b64decode(sys.argv[1]))";
const LAUNCH_TIMEOUT: Duration = Duration::from_secs(180);

// The installed peer still speaks the legacy protocol, including the login shell
// and the single-line bootstrap required by the Windows SSH hop.
const RECEIVER_HEADER: &str = "import json,os,pathlib,subprocess,sys\np=json.load(sys.stdin)\n";
const RECEIVER_BODY: &str = r#"state=pathlib.Path(os.environ.get("ROUTING_STATE_DIR") or pathlib.Path.home()/".local/state/routing")/"launch"
a=p["argv"]
env=dict(os.environ,FLEET_LOCAL_HOST=p["host"],FLEET_FORWARDED="1")
import shutil
exe=shutil.which("pg-spawnd")
if not exe:
 print("fleet-launch: refused host "+p["host"]+": pg-spawnd is not installed there; no launch",file=sys.stderr)
 sys.exit(2)
launcher=[exe,"session"]
env.setdefault("PLANGRAPH_FLEET_OPS",str(root/"ops"))
role=a[0].rsplit("-",1)
role=role[0] if len(role)==2 and role[1].isdigit() and len(role[1])>=4 else a[0]
saved=state/"sessions"/((a[a.index("--session")+1] if "--session" in a else a[0])+".json")
if "--respawn" in a and not p["spec"] and not saved.exists() and not (state/(role+".json")).exists():
 print("fleet-launch: refused host "+p["host"]+": missing launch spec; no launch",file=sys.stderr)
 sys.exit(2)
if p.get("seq"):
 env["FLEET_SESSION_SEQ"]=str(p["seq"])
dry="--dry-run" in a
if dry:
 import tempfile
 work=pathlib.Path(tempfile.mkdtemp())
else:
 work=state
 state.mkdir(parents=True,exist_ok=True)
prompt=work/(a[0]+".remote-prompt.md")
prompt.write_text(p["prompt"])
mcp=work/(a[0]+".remote-mcp.json")
mcp.write_text(p["mcp"])
a[4]=str(prompt)
a += ["--mcp",str(mcp)]
if dry:
 rc=subprocess.call(launcher+a,env=env)
 shutil.rmtree(work,ignore_errors=True)
 sys.exit(rc)
if p["spec"]:
 targets=[state/(role+".json")]
 if "--session" in a:
  (state/"sessions").mkdir(exist_ok=True)
  targets.append(state/"sessions"/(a[a.index("--session")+1]+".json"))
 for target in targets:
  if not target.exists():
   target.write_text(json.dumps(p["spec"]))
rc=subprocess.call(launcher+a,env=env)
if rc == 0:
 print("FLEET_REMOTE_SPEC="+(state/(role+".json")).read_text().replace(chr(10), ""))
sys.exit(rc)
"#;

fn send(request: &Request, paths: &Paths, host: &Host) -> Result<Output, String> {
    let args = &request.args;
    let seq = if args.respawn || args.dry {
        None
    } else {
        Some(next_seq(&paths.state).map_err(|e| io_text(&e, &paths.state.join("session-seq")))?)
    };
    let mut argv = args.raw.clone();
    argv.extend(["--host".into(), host.name().into()]);
    let prompt = Path::new(&args.prompt);
    let prompt = if let Some(restart) = request.restart.as_ref() {
        argv.extend([
            "--card".into(),
            restart.card.to_string_lossy().into_owned(),
            "--handoff".into(),
            restart.handoff.to_string_lossy().into_owned(),
        ]);
        if let Some(caps) = request
            .override_cap
            .as_ref()
            .and_then(Caps::from_value)
            .filter(|_| args.guard.is_none())
        {
            argv.extend([
                "--ctx-guard".into(),
                format!("{},{}", caps.handoff, caps.hard),
            ]);
        }
        restart.prompt.clone()
    } else {
        read_writer_prompt(prompt).map_err(|e| io_text(&e, prompt))?
    };
    let mcp = args
        .option("mcp")
        .filter(|s| !s.is_empty())
        .map_or_else(|| paths.mcp(), Into::into);
    let payload = json!({
        "host": host.name(), "argv": argv, "spec": request.spec, "seq": seq,
        "prompt": prompt,
        "mcp": read_text(&mcp).map_err(|e| io_text(&e, &mcp))?,
    });
    // The path is installation data. The receiver keeps the old bytes when
    // the candidate has the same layout as the installed fleet launcher.
    let router = paths.remote_router()?;
    let router = serde_json::to_string(&router).expect("serialize path string");
    let receiver = format!("{RECEIVER_HEADER}root=pathlib.Path.home()/{router}\n{RECEIVER_BODY}");
    let suffix = format!(
        "python3 -c {} {}",
        shell_quote(LOADER),
        STANDARD.encode(receiver)
    );
    let mut command = host.bash(&suffix).map_err(|e| e.to_string())?;
    transmit(
        &mut command,
        preserve_order(&payload).as_bytes(),
        LAUNCH_TIMEOUT,
    )
}

fn transmit(
    command: &mut std::process::Command,
    payload: &[u8],
    timeout: Duration,
) -> Result<Output, String> {
    capture(command, Some(payload), timeout).map_err(|error| {
        if error.kind() == io::ErrorKind::TimedOut {
            let argv = std::iter::once(command.get_program())
                .chain(command.get_args())
                .map(|arg| repr(&arg.to_string_lossy()))
                .collect::<Vec<_>>()
                .join(", ");
            let seconds = timeout.as_secs_f64();
            format!("Command '[{argv}]' timed out after {seconds} seconds")
        } else {
            io_text(&error, Path::new("ssh"))
        }
    })
}

pub(super) fn launch(
    request: &Request,
    paths: &Paths,
    host: &Host,
    out: &mut dyn Write,
) -> io::Result<i32> {
    let output = match send(request, paths, host) {
        Ok(output) => output,
        Err(error) => {
            eprintln!(
                "fleet-launch: host {} launch uncertain; do not retry: {error}",
                host.name()
            );
            return Ok(2);
        }
    };
    let stdout = normalize(&String::from_utf8_lossy(&output.stdout));
    let mut forwarded = String::new();
    for line in stdout.lines() {
        if let Some(data) = line.strip_prefix("FLEET_REMOTE_SPEC=") {
            let mut spec: Value = serde_json::from_str(data)?;
            if !spec.is_object() {
                return Err(io::Error::other("invalid remote launch spec"));
            }
            mirror(request, paths, host, &mut spec)?;
        } else {
            forwarded.push_str(line);
            forwarded.push('\n');
        }
    }
    write!(out, "{forwarded}")?;
    io::stderr().write_all(normalize(&String::from_utf8_lossy(&output.stderr)).as_bytes())?;
    Ok(exit_code(&output))
}

fn mirror(request: &Request, paths: &Paths, host: &Host, spec: &mut Value) -> io::Result<()> {
    spec["host"] = json!(host.name());
    let target = record_path(request);
    fs::create_dir_all(paths.state.join("launch"))?;
    fs::write(&target, preserve_order(spec))?;
    if let Some(name) = spec["session_name"].as_str().filter(|n| basename(n)) {
        let mut snapshot = spec.clone();
        snapshot["prompt_file"] = json!(resolve(Path::new(&request.args.prompt)));
        let dir = paths.state.join("launch/sessions");
        fs::create_dir_all(&dir)?;
        fs::write(dir.join(format!("{name}.json")), preserve_order(&snapshot))?;
        if target == request.spec_path {
            register_child(&paths.state, name, spec);
        }
    }
    Ok(())
}

pub(super) fn exit_code(output: &Output) -> i32 {
    output
        .status
        .code()
        .unwrap_or_else(|| -output.status.signal().unwrap_or(1))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;

    #[test]
    fn forward_timeout_keeps_the_transport_command_in_the_diagnostic() {
        let error = transmit(
            Command::new("/usr/bin/sleep").arg("10"),
            b"{}",
            Duration::from_millis(20),
        )
        .unwrap_err();
        assert_eq!(LAUNCH_TIMEOUT, Duration::from_secs(180));
        assert_eq!(
            error,
            "Command '['/usr/bin/sleep', '10']' timed out after 0.02 seconds"
        );
    }
}
