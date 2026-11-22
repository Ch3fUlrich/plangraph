use super::{
    arguments::Arguments,
    paths::{basename, Paths},
    policy,
    restart::Restart,
    role_of,
};
use serde_json::{json, Value};
use std::path::PathBuf;

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
        })
    }
}
