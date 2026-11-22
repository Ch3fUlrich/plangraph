use super::{arguments::METADATA, paths::resolve, Caps};
use serde_json::{json, Map, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

const DEFAULT_CAPS: Caps = Caps {
    handoff: 250000,
    hard: 300000,
};

pub(super) struct ActiveCaps {
    pub caps: Caps,
    pub source: &'static str,
    pub window: u64,
    pub error: Option<&'static str>,
}

pub(super) fn load(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

pub(super) fn refused_role<'a>(policy: Option<&'a Value>, role: &str) -> Option<&'a str> {
    policy?
        .get("refuse_roles")?
        .as_array()?
        .iter()
        .filter_map(Value::as_str)
        .find(|pattern| {
            fancy_regex::Regex::new(pattern).is_ok_and(|re| re.is_match(role).unwrap_or(false))
        })
}

pub(super) fn select_caps(
    policy: Option<&Value>,
    role: &str,
    override_cap: Option<&Value>,
) -> ActiveCaps {
    let mut default = DEFAULT_CAPS;
    let mut active = ActiveCaps {
        caps: default,
        source: "default",
        window: 1800,
        error: None,
    };
    if let Some(policy) = policy.filter(|p| p.is_object()) {
        if let Some(candidate) = policy.pointer("/caps/default") {
            if let Some(cap) = Caps::from_value(candidate) {
                default = cap;
            } else {
                active.error = Some("invalid default cap; using default");
            }
        }
        active.caps = default;
        let roles = policy.pointer("/caps/roles");
        if policy.get("caps").is_some_and(|c| !c.is_object())
            || roles.is_some_and(|r| !r.is_object())
        {
            active.error = Some("invalid cap policy; using default");
        } else {
            if let Some(candidate) = roles.and_then(|roles| roles.get(role)) {
                if let Some(cap) = Caps::from_value(candidate) {
                    active.caps = cap;
                    active.source = "role";
                } else {
                    active.error = Some("invalid role cap; using default");
                }
            }
            active.window = attended_window(policy.get("attended_window_s"));
        }
    } else {
        active.error = Some("invalid cap policy; using default");
    }
    if let Some(candidate) = override_cap {
        if let Some(cap) = Caps::from_value(candidate) {
            active.caps = cap;
            active.source = "override";
            active.error = None;
        } else {
            active.caps = default;
            active.source = "default";
            active.error = Some("invalid_override");
        }
    }
    active
}

pub(super) fn attended_window(value: Option<&Value>) -> u64 {
    value
        .and_then(Value::as_u64)
        .filter(|w| (1..=3600).contains(w))
        .unwrap_or(1800)
}

pub(crate) fn is_attended(marker: &Path, window: u64, now: f64) -> bool {
    use std::time::UNIX_EPOCH;
    let issued = (|| {
        let modified = fs::metadata(marker).ok()?.modified().ok()?;
        Some(match modified.duration_since(UNIX_EPOCH) {
            Ok(duration) => duration.as_secs_f64(),
            Err(error) => -error.duration().as_secs_f64(),
        })
    })();
    let (Some(issued), Some(doc)) = (issued, load(marker)) else {
        return false;
    };
    if issued > now {
        let _ = fs::remove_file(marker);
        return false;
    }
    let window = attended_window(Some(&json!(window))) as f64;
    doc["until"]
        .as_f64()
        .is_some_and(|until| issued <= now && now < until && until <= issued + window)
}

pub(super) struct Admission {
    pub caps: ActiveCaps,
    pub fields: Map<String, Value>,
    pub state: PathBuf,
    pub ledger: PathBuf,
    pub allowed: bool,
    pub enforce: bool,
    pub missing: Vec<&'static str>,
    pub error: Option<&'static str>,
}

impl Admission {
    pub fn check(
        request: &super::Request,
        policy: Option<&Value>,
        host: Option<&str>,
        state: &Path,
    ) -> Self {
        let fields: Map<String, Value> = METADATA
            .into_iter()
            .map(|key| {
                let option = if key == "host" {
                    host.or(request.args.option(key))
                } else {
                    request.args.option(key)
                };
                let value = option.map_or_else(|| request.spec[key].clone(), |value| json!(value));
                (key.to_owned(), value)
            })
            .chain([
                ("role".into(), json!(request.args.role)),
                ("effort".into(), json!(request.args.effort)),
            ])
            .collect();
        let is_present = |key: &str| present(&fields[key]);
        let mut missing: Vec<_> = ["role", "owner"]
            .into_iter()
            .filter(|key| !is_present(key))
            .collect();
        if !is_present("card") && !is_present("track") {
            missing.push("card or track");
        }
        missing.extend(
            ["host", "reason", "effort", "cap", "handoff"]
                .into_iter()
                .filter(|key| !is_present(key)),
        );
        let state = resolve(state);
        let configured = policy.and_then(|p| {
            let enforce = p.get("enforce")?.as_bool()?;
            let ledger = p.get("ledger")?.as_str().filter(|s| !s.trim().is_empty())?;
            let ledger = resolve(&state.join(ledger));
            (ledger.starts_with(&state) && ledger != state).then_some((ledger, enforce))
        });
        let error = configured.is_none().then_some("missing or invalid policy");
        let (ledger, enforce) =
            configured.unwrap_or_else(|| (state.join("fleet-launch-ledger.jsonl"), false));
        let caps = select_caps(policy, &request.args.role, request.override_cap.as_ref());
        let allowed = !enforce || (missing.is_empty() && caps.error != Some("invalid_override"));
        Self {
            caps,
            fields,
            state,
            ledger,
            allowed,
            enforce,
            missing,
            error,
        }
    }

    pub fn message(&self, legacy: bool) -> String {
        let mut parts: Vec<String> = [self.error, self.caps.error]
            .into_iter()
            .flatten()
            .map(str::to_owned)
            .collect();
        if !self.missing.is_empty() {
            parts.push(format!("missing: {}", self.missing.join(", ")));
            if legacy {
                parts.push("warning: respawn preserves legacy admission metadata".into());
            }
        }
        parts.join("; ")
    }
}

fn present(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.trim().is_empty())
}
