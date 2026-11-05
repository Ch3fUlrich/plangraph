//! Unwired retention evidence only; FLEET-RECYCLE-RETAIN. Adapters and observation time are injected.
use crate::fleet_owner_lease::{Identity, Read, State};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum HostClass {
    DirectLinux,
    RemoteShellLinux,
    Windows,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Capabilities {
    pub panes: bool,
    pub remote_control: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Transcript {
    Readable,
    Missing,
    Unknown(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Pane {
    Retained,
    Gone,
    NotApplicable,
    Unknown(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RemoteControl {
    Attached,
    Detached,
    NotApplicable,
    Unknown(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum OomScope {
    Groups(BTreeSet<u32>),
    Unknown(String),
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Observation {
    pub transcript: Transcript,
    pub pane: Pane,
    pub remote_control: RemoteControl,
    pub oom_scope: OomScope,
}
/// The caller supplies a class-specific observation adapter, never lifecycle authority.
pub trait RetentionAdapter {
    fn host_class(&self) -> HostClass;
    fn capabilities(&self) -> Capabilities;
    fn observe(&self, session: &str) -> Observation;
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Finding {
    pub session_id: String,
    pub field: String,
    pub reason: String,
    pub alarm: bool,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Report {
    pub old_session: String,
    pub new_session: String,
    pub observed_at_s: i64,
    pub host_class: HostClass,
    pub capabilities: Capabilities,
    pub owner: Option<Identity>,
    pub observation: Observation,
    pub findings: Vec<Finding>,
    pub passed: bool,
}
pub fn check(
    adapter: &(impl RetentionAdapter + ?Sized),
    lease: &Read,
    old_session: &str,
    new_session: &str,
    now_s: i64,
) -> Report {
    let observation = adapter.observe(old_session);
    let capabilities = adapter.capabilities();
    let mut findings = Vec::new();
    let mut finding = |field: &str, reason: &str, alarm| {
        findings.push(Finding {
            session_id: old_session.into(),
            field: field.into(),
            reason: reason.into(),
            alarm,
        });
    };
    if old_session == new_session {
        finding(
            "session",
            "successor must have a distinct session id",
            false,
        );
    }
    let owner = match lease {
        Read::Known(record) => {
            if record.session_id != old_session {
                finding("lease", "session identity mismatch", false);
            }
            if record.validate(now_s).is_err() {
                finding("lease", "invalid record or observation time", true);
            }
            match &record.identity {
                State::Busy(owner) => Some(owner.clone()),
                _ => {
                    finding("lease", "session owner identity unavailable", true);
                    None
                }
            }
        }
        Read::Unknown { reason, .. } => {
            finding("lease", reason, true);
            None
        }
    };
    match &observation.transcript {
        Transcript::Readable => (),
        Transcript::Missing => finding("transcript", "transcript missing", false),
        Transcript::Unknown(reason) => finding("transcript", reason, true),
    }
    match &observation.pane {
        Pane::Retained if capabilities.panes => (),
        Pane::NotApplicable if !capabilities.panes => (),
        Pane::Unknown(reason) => finding("pane", reason, true),
        _ => finding("pane", "pane not retained or applicability mismatch", false),
    }
    match &observation.remote_control {
        RemoteControl::Attached | RemoteControl::Detached if capabilities.remote_control => (),
        RemoteControl::NotApplicable if !capabilities.remote_control => (),
        RemoteControl::Unknown(reason) => finding("remote_control", reason, true),
        _ => finding(
            "remote_control",
            "remote control applicability mismatch",
            false,
        ),
    }
    match &observation.oom_scope {
        OomScope::Unknown(reason) => finding("oom_scope", reason, true),
        OomScope::Groups(groups) => {
            if let Some(owner) = &owner {
                if groups != &BTreeSet::from([owner.group]) {
                    finding(
                        "oom_scope",
                        "scope must equal the session owner group",
                        false,
                    );
                }
            } else {
                finding(
                    "oom_scope",
                    "scope cannot be verified without the session owner group",
                    true,
                );
            }
        }
    }
    Report {
        old_session: old_session.into(),
        new_session: new_session.into(),
        observed_at_s: now_s,
        host_class: adapter.host_class(),
        capabilities,
        owner,
        observation,
        passed: findings.is_empty(),
        findings,
    }
}

#[cfg(test)]
#[path = "fleet_recycle_retain_tests.rs"]
mod tests;
