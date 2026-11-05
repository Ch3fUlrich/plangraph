//! Pure S12 policy contract; no caller integration or live activation.
use crate::fleet_window_catalog::{self as catalog, Capture, Finding, Window};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Activation {
    pub name: String,
    pub sha: String,
    pub decision_run: String,
    pub at_s: i64,
    pub reviewed_rollback: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum UnknownPolicy {
    KeepPriorAndAlarm,
    CapAtConfiguredAndAlarm(u64),
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Policy {
    pub numerator: u64,
    pub denominator: u64,
    pub unknown: UnknownPolicy,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Resolution {
    pub threshold: Option<u64>,
    pub window: Window,
    pub source: String,
    pub provenance: Value,
    pub activation: Option<Activation>,
    pub catalog: Option<Capture>,
    pub alarms: Vec<Finding>,
}
/// Exact identity and current immutable commit are supplied by the later caller.
/// Acquisition errors are retained; serialized captures are decoded again, not trusted.
pub fn resolve(
    identity: &Value,
    bytes: Result<&[u8], &str>,
    now_s: i64,
    current_sha: &str,
    prior: Option<u64>,
    policy: Option<&Policy>,
    activation: Option<&Activation>,
) -> Resolution {
    let mut out = Resolution {
        threshold: prior,
        window: Window::Unknown("not_activated".into()),
        source: "prior".into(),
        provenance: Value::Null,
        activation: activation.cloned(),
        catalog: None,
        alarms: vec![],
    };
    let valid = activation.is_some_and(|a| {
        a.name == "ENABLE-FLEET-CAP-RESOLVER"
            && a.sha == current_sha
            && matches!(a.sha.len(), 40 | 64)
            && a.sha.bytes().all(|b| b.is_ascii_hexdigit())
            && !a.decision_run.trim().is_empty()
            && !a.reviewed_rollback.trim().is_empty()
            && a.at_s >= 0
            && a.at_s <= now_s
    });
    let alarm = |out: &mut Resolution, source: &str, reason: &str| {
        out.alarms.push(Finding {
            identity: identity.clone(),
            source: source.into(),
            reason: reason.into(),
        });
    };
    let capture = match bytes {
        Ok(b) => catalog::decode(b, now_s),
        Err(reason) => {
            alarm(&mut out, catalog::RELATIVE_PATH, reason);
            catalog::decode(b"", now_s)
        }
    };
    out.alarms.extend(capture.findings.iter().map(|f| Finding {
        identity: f.identity.clone(),
        source: f.source.clone(),
        reason: f.reason.clone(),
    }));
    if !valid {
        out.catalog = Some(capture);
        alarm(&mut out, "activation", "missing_or_invalid_activation");
        return out;
    }
    let records: Vec<_> = capture
        .records
        .iter()
        .filter(|r| r.identity == *identity)
        .collect();
    out.window = if records.len() == 1 {
        let record = records[0];
        out.source = record.selected_source.clone();
        out.provenance = capture.evidence["entries"]
            .as_array()
            .and_then(|entries| entries.iter().find(|e| e["identity"] == *identity))
            .map(|e| e[&record.selected_source].clone())
            .unwrap_or(Value::Null);
        record.window.clone()
    } else {
        out.source = catalog::RELATIVE_PATH.into();
        Window::Unknown("missing_or_nonunique_identity".into())
    };
    out.catalog = Some(capture);
    let Some(policy) = policy else {
        out.source = "policy:absent_or_invalid".into();
        out.provenance = Value::Null;
        out.window = Window::Unknown("absent_policy".into());
        alarm(&mut out, "policy", "absent_policy");
        return out;
    };
    let invalid = policy.numerator == 0
        || policy.denominator == 0
        || policy.numerator > policy.denominator
        || matches!(policy.unknown, UnknownPolicy::CapAtConfiguredAndAlarm(0));
    if invalid {
        out.source = "policy:absent_or_invalid".into();
        out.provenance = Value::Null;
        out.window = Window::Unknown("invalid_policy".into());
        alarm(&mut out, "policy", "invalid_policy");
        return out;
    }
    if let Window::Known(n) = out.window {
        if let Some(threshold) = n
            .checked_mul(policy.numerator)
            .and_then(|n| n.checked_div(policy.denominator))
            .filter(|n| *n > 0)
        {
            out.threshold = Some(threshold);
            return out;
        }
        out.window = Window::Unknown("policy_arithmetic".into());
        alarm(&mut out, "policy", "overflow_or_zero_output");
    }
    let reason = unknown_reason(&out.window);
    alarm(&mut out, "unknown_policy", &reason);
    out.source = match policy.unknown {
        UnknownPolicy::KeepPriorAndAlarm => "policy:KeepPriorAndAlarm",
        UnknownPolicy::CapAtConfiguredAndAlarm(cap) => {
            out.threshold = Some(prior.map_or(cap, |n| n.min(cap)));
            "policy:CapAtConfiguredAndAlarm"
        }
    }
    .into();
    if out.threshold.is_none() {
        alarm(&mut out, "unknown_policy", "unresolved_no_threshold");
    }
    out
}
fn unknown_reason(window: &Window) -> String {
    match window {
        Window::Unknown(reason) => reason.clone(),
        _ => "unsupported_window".into(),
    }
}
#[cfg(test)]
#[path = "fleet_cap_resolver_tests.rs"]
mod tests;
