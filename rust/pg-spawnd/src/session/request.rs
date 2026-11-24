use super::{
    arguments::Arguments,
    paths::{basename, Paths},
    policy,
    restart::Restart,
    role_of,
};
use serde_json::{json, Value};
use std::path::PathBuf;

/// The saved session a worker respawn continues: its own name and transcript id.
pub(super) struct Resume {
    pub name: String,
    pub session_id: String,
}

pub(super) struct Request {
    pub args: Arguments,
    pub spec: Value,
    pub spec_path: PathBuf,
    pub legacy_respawn: bool,
    pub override_cap: Option<Value>,
    pub restart: Option<Restart>,
    pub resume: Option<Resume>,
}

impl Request {
    pub fn new(args: Arguments, paths: &Paths) -> Option<Self> {
        let launch = paths.state.join("launch");
        let spec_path = launch.join(format!("{}.json", args.role));
        let mut spec = json!({});
        let mut legacy_respawn = false;
        if args.respawn {
            let source = args.option("session").unwrap_or(&args.name);
            let from = if source == args.role {
                legacy_respawn = true;
                spec_path.clone()
            } else {
                if role_of(source) != args.role || !basename(source) {
                    eprintln!("fleet-launch: invalid respawn session");
                    return None;
                }
                launch.join("sessions").join(format!("{source}.json"))
            };
            if let Some(saved) = policy::load(&from).filter(Value::is_object) {
                spec = saved;
                legacy_respawn = true;
            }
        }
        let override_cap = args.guard.map(|guard| json!(guard)).or_else(|| {
            (spec["ctx_guard_source"] == "operator")
                .then(|| spec["ctx_guard"].clone())
                .filter(|cap| !cap.is_null())
        });
        Some(Self {
            args,
            spec,
            spec_path,
            legacy_respawn,
            override_cap,
            restart: None,
            resume: None,
        })
    }

    /// A respawn of a numbered worker session continues the saved transcript under the same
    /// name; any other respawn starts a fresh session from its handoff.
    pub fn resume_target(&self, worker: bool) -> Option<Resume> {
        let args = &self.args;
        let source = args.option("session").unwrap_or(&args.name);
        let id = self.spec["session_id"].as_str()?;
        let plain = |c: char| c.is_ascii_alphanumeric() || c == '-';
        (args.respawn
            && worker
            && source != args.role
            && role_of(source) == args.role
            && basename(source)
            && !id.starts_with('-')
            && !id.is_empty()
            && id.chars().all(plain))
        .then(|| Resume {
            name: source.to_owned(),
            session_id: id.to_owned(),
        })
    }
}
