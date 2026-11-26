use super::{
    arguments::{Arguments, ParseError, USAGE},
    paths::{basename, repr, resolve, var, Paths},
    placement::{Host, Placement},
    policy::{self, refused_role, Admission},
    remote, restart,
    restart::Restart,
    role_of, round_state, round_watch, storage,
};
use serde_json::{json, Value};
use std::{
    io::{self, Write},
    path::{Path, PathBuf},
};

pub(super) const FORWARDED_NUMBERED_RESUME: &str = "--forwarded-numbered-resume";

pub(super) struct ResumeIdentity {
    pub name: String,
    pub seq: u64,
    pub id: String,
}

pub(super) struct Request {
    pub args: Arguments,
    pub spec: Value,
    pub spec_path: PathBuf,
    pub legacy_respawn: bool,
    pub override_cap: Option<Value>,
    pub restart: Option<Restart>,
}

impl Request {
    pub fn new(args: Arguments, paths: &Paths) -> Option<Self> {
        let launch = paths.state.join("launch");
        let spec_path = launch.join(format!("{}.json", args.role));
        if args.respawn {
            let source = args.option("session").unwrap_or(&args.name);
            if source != args.role && (role_of(source) != args.role || !basename(source)) {
                eprintln!("fleet-launch: invalid respawn session");
                return None;
            }
        }
        let mut request = Self {
            args,
            spec: json!({}),
            spec_path,
            legacy_respawn: false,
            override_cap: None,
            restart: None,
        };
        request.reload(paths);
        Some(request)
    }

    fn reload(&mut self, paths: &Paths) {
        if self.args.respawn {
            let source = self.source();
            let by_role = source == self.args.role;
            let saved = if by_role {
                policy::load(&self.spec_path)
            } else {
                policy::load(
                    &paths
                        .state
                        .join("launch/sessions")
                        .join(format!("{source}.json")),
                )
            };
            let saved = saved.filter(Value::is_object);
            self.legacy_respawn = by_role || saved.is_some();
            self.spec = saved.unwrap_or_else(|| json!({}));
        }
        self.override_cap = self.args.guard.map(|guard| json!(guard)).or_else(|| {
            (self.spec["ctx_guard_source"] == "operator")
                .then(|| self.spec["ctx_guard"].clone())
                .filter(|cap| !cap.is_null())
        });
    }

    fn source(&self) -> &str {
        self.args.option("session").unwrap_or(&self.args.name)
    }

    pub fn numbered_writer_name(&self) -> Option<&str> {
        let source = self.source();
        (self.args.respawn && self.args.role == "writer" && source != self.args.role)
            .then_some(source)
    }

    fn resume_identity(&self, host: &Host, placement: &Placement) -> Option<ResumeIdentity> {
        let name = self.numbered_writer_name()?;
        let seq = name
            .strip_prefix("writer-")?
            .parse()
            .ok()
            .filter(|&seq| seq > 0)?;
        let cwd = self.spec["cwd"].as_str().filter(|cwd| !cwd.is_empty())?;
        let id = self.spec["session_id"].as_str().filter(|id| valid_id(id))?;
        let same_host = self.spec["host"]
            .as_str()
            .filter(|s| !s.is_empty())
            .is_none_or(|saved| placement.saved_local(saved, host));
        (self.spec["session_name"] == name
            && same_host
            && resolve(Path::new(cwd)) == resolve(Path::new(&self.args.cwd)))
        .then(|| ResumeIdentity {
            name: name.to_owned(),
            seq,
            id: id.to_owned(),
        })
    }

    fn refuse(&self, paths: &Paths, policy: Option<&Value>, reason: &str) -> bool {
        if !self.args.dry {
            let admission = Admission::check(self, policy, None, &paths.state);
            storage::record_outcome(&admission, "refused", None);
        }
        eprintln!("fleet-launch: {reason}");
        false
    }

    fn prepare(&mut self, paths: &Paths, policy: Option<&Value>) -> bool {
        if let Some(reason) = round_blocked(self, &round_watch::spawn_state_dir()) {
            return self.refuse(
                paths,
                policy,
                &format!("round BLOCKED, no automatic restart: {reason}"),
            );
        }
        match restart::prepare(self, paths, policy) {
            Ok(restart) => {
                self.restart = restart;
                true
            }
            Err(reason) => self.refuse(paths, policy, &format!("PROMPT-STALE {reason}")),
        }
    }
}

fn valid_id(id: &str) -> bool {
    const UUID_PARTS: [usize; 5] = [8, 4, 4, 4, 12];
    let parts = id.split('-').collect::<Vec<_>>();
    parts.len() == UUID_PARTS.len()
        && parts.iter().zip(UUID_PARTS).all(|(part, width)| {
            part.len() == width && part.bytes().all(|byte| byte.is_ascii_hexdigit())
        })
}

pub(super) fn launch(mut args: Vec<String>, paths: &Paths, out: &mut dyn Write) -> io::Result<i32> {
    let forwarded = var("FLEET_FORWARDED").is_some();
    // Old destination binaries treat the required marker as an extra argument
    // and refuse the launch rather than accidentally opening a new session.
    let peer_resume = forwarded
        && args
            .first()
            .is_some_and(|arg| arg == FORWARDED_NUMBERED_RESUME);
    if peer_resume {
        args.remove(0);
    }
    let mut args = match Arguments::parse(args) {
        Ok(args) => args,
        Err(ParseError::Usage) => {
            writeln!(out, "{}", USAGE)?;
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
    if peer_resume {
        args.prompt = "/dev/stdin".into();
    }
    let policy_path = paths.ops.join("fleet-launch-policy.json");
    let policy = policy::load(&policy_path);
    if let Some(pattern) = refused_role(policy.as_ref(), &args.role) {
        eprintln!("fleet-launch: role {} is refused by policy ({pattern}); no launch: dispatch the work as spawner runs from the L1", args.role);
        return Ok(2);
    }
    let Some(mut request) = Request::new(args, paths) else {
        return Ok(2);
    };
    let numbered = request.numbered_writer_name().is_some();
    if peer_resume && !numbered {
        eprintln!("fleet-launch: forwarded resume requires a numbered writer");
        return Ok(2);
    }
    if !numbered && !request.prepare(paths, policy.as_ref()) {
        return Ok(2);
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
        if peer_resume {
            eprintln!(
                "fleet-launch: forwarded resume refused: {} is not local",
                host.name()
            );
            return Ok(2);
        }
        return remote::launch(&request, paths, &host, out);
    }
    // Keep the selected record stable across local, live resume and publication.
    let _lock = if numbered && !request.args.dry {
        match storage::role_lock(&paths.state, &request.args.role) {
            Ok(lock) => {
                request.reload(paths);
                Some(lock)
            }
            Err(error) => {
                request.refuse(paths, policy.as_ref(), &format!("{error}; no launch"));
                return Ok(2);
            }
        }
    } else {
        None
    };
    let resume = if let Some(name) = request.numbered_writer_name() {
        let Some(identity) = request.resume_identity(&host, &placement) else {
            request.refuse(
                paths,
                policy.as_ref(),
                &format!(
                    "missing saved session {name} for {} on {}; no launch",
                    request.args.cwd,
                    host.name()
                ),
            );
            return Ok(2);
        };
        if !request.prepare(paths, policy.as_ref()) {
            return Ok(2);
        }
        Some(identity)
    } else {
        None
    };
    let admission = Admission::check(
        &request,
        policy.as_ref(),
        placement.policy_host(&host),
        &paths.state,
    );
    super::local(
        &request,
        paths,
        &admission,
        host.name(),
        resume.as_ref(),
        out,
    )
}

/// A session whose round the wait watchdog closed as BLOCKED is not restarted on its own.
/// The block is stored under the numbered session name; a role-only respawn reaches that
/// name through the saved launch record it restarts from.
pub(super) fn round_blocked(request: &Request, spawn_state: &Path) -> Option<String> {
    let args = &request.args;
    let source = request.source();
    let saved = request.spec["session_name"].as_str();
    blocked_reason(args.respawn, &[Some(source), saved], spawn_state)
}

pub(super) fn blocked_reason(
    respawn: bool,
    names: &[Option<&str>],
    spawn_state: &Path,
) -> Option<String> {
    if !respawn {
        return None;
    }
    names
        .iter()
        .flatten()
        .filter(|name| basename(name))
        .find_map(|name| {
            let path = round_watch::state_file(spawn_state, name);
            let blocked = round_watch::blocked_outcome(&path)?;
            Some(format!(
                "{name}: {} (command `{}`); clear {} after the operator has looked",
                blocked["reason"].as_str().unwrap_or("blocked"),
                // Records written before the command was redacted at write time are redacted here.
                round_state::redacted(blocked["command"].as_str().unwrap_or("?")),
                path.display()
            ))
        })
}
