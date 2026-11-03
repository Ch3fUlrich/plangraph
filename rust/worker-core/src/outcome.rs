//! Pure classification of structured attempt facts; no caller integration yet.
use crate::{activity::KillOutcome, hosts::Role, redact::redact, reset::Reset};
pub mod markers;

#[cfg(test)]
mod tests;
#[cfg(test)]
mod usable_tests;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Terminal {
    CallerFault,
    Cancelled,
    Orphaned,
    GitConfigMutated,
    WriterNoChanges,
    KillIncomplete,
    ProofFailure,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OwnKill {
    Monitor(KillOutcome),
    MaxS,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Marker {
    Quota,
    RateLimit,
    Auth,
    ModelUnavailable,
    ProviderUnavailable,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Scope {
    Leg,
    LegReview,
    Route,
    None,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Ok,
    HangNoActivity,
    HangSilent,
    NoProgress,
    MaxTime,
    KilledExternal,
    Empty,
    Crash,
    ToolCallOnly,
    NoVerdict,
    ModelUnavailable,
    ProviderUnavailable,
    Quota,
    RateLimit,
    Auth,
    Neutral(Terminal),
}
impl Class {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "OK",
            Self::HangNoActivity => "HANG_NO_ACTIVITY",
            Self::HangSilent => "HANG_SILENT",
            Self::NoProgress => "NO_PROGRESS",
            Self::MaxTime => "MAX_TIME",
            Self::KilledExternal => "KILLED_EXTERNAL",
            Self::Empty => "EMPTY",
            Self::Crash => "CRASH",
            Self::ToolCallOnly => "TOOL_CALL_ONLY",
            Self::NoVerdict => "NO_VERDICT",
            Self::ModelUnavailable => "MODEL_UNAVAILABLE",
            Self::ProviderUnavailable => "PROVIDER_UNAVAILABLE",
            Self::Quota => "QUOTA",
            Self::RateLimit => "RATE_LIMIT",
            Self::Auth => "AUTH",
            Self::Neutral(t) => match t {
                Terminal::CallerFault => "caller_fault",
                Terminal::Cancelled => "cancelled",
                Terminal::Orphaned => "orphaned",
                Terminal::GitConfigMutated => "git_config_mutated",
                Terminal::WriterNoChanges => "writer_no_changes",
                Terminal::KillIncomplete => "kill_incomplete",
                Terminal::ProofFailure => "proof_failure",
            },
        }
    }
    pub fn is_neutral(self) -> bool {
        matches!(self, Self::MaxTime | Self::Neutral(_))
    }
    pub fn scope(self) -> Scope {
        match self {
            Self::Auth | Self::ProviderUnavailable => Scope::Route,
            Self::ToolCallOnly | Self::NoVerdict => Scope::LegReview,
            Self::Neutral(_) => Scope::None,
            _ => Scope::Leg,
        }
    }
    pub fn carries_reset(self) -> bool {
        matches!(
            self,
            Self::Quota
                | Self::RateLimit
                | Self::Auth
                | Self::ModelUnavailable
                | Self::ProviderUnavailable
        )
    }
}

#[derive(Debug)]
pub struct Facts {
    pub role: Role,
    pub exit: i32,
    pub terminal: Option<Terminal>,
    pub own_kill: Option<OwnKill>,
    pub marker: Option<Marker>,
    pub content: bool,
    pub tool: bool,
    pub tree_changed: bool,
    pub verdict_usable: bool,
    pub tool_call_only: bool,
    pub reset: Option<Reset>,
    pub evidence: Option<String>,
}
impl Facts {
    pub fn new(role: Role, exit: i32) -> Self {
        Self {
            role,
            exit,
            terminal: None,
            own_kill: None,
            marker: None,
            content: false,
            tool: false,
            tree_changed: false,
            verdict_usable: false,
            tool_call_only: false,
            reset: None,
            evidence: None,
        }
    }
}
#[derive(Debug)]
pub struct Outcome {
    pub class: Class,
    pub reset_at: Option<f64>,
    pub reset_clamped: bool,
    pub evidence: Option<String>,
}
impl Outcome {
    pub fn describe(&self) -> String {
        match &self.evidence {
            None => self.class.as_str().to_owned(),
            Some(text) => {
                let safe = redact(text);
                let line: String = safe
                    .lines()
                    .next()
                    .unwrap_or("")
                    .chars()
                    .take(300)
                    .collect();
                format!("{}: {line}", self.class.as_str())
            }
        }
    }
}
pub fn usable(f: &Facts) -> bool {
    match f.role {
        Role::Implement => f.tree_changed,
        Role::Review => f.verdict_usable,
        Role::Orchestrate | Role::Research | Role::Session => f.content,
    }
}

pub fn classify(f: &Facts) -> Outcome {
    let finish = |class: Class| {
        let reset = f.reset.as_ref().filter(|_| class.carries_reset());
        Outcome {
            class,
            reset_at: reset.map(|r| r.reset_at),
            reset_clamped: reset.is_some_and(|r| r.clamped),
            evidence: f.evidence.clone(),
        }
    };
    if let Some(t) = f.terminal {
        return finish(Class::Neutral(t));
    }
    if let Some(kill) = f.own_kill {
        return finish(match kill {
            OwnKill::Monitor(KillOutcome::HangNoActivity) => Class::HangNoActivity,
            OwnKill::Monitor(KillOutcome::HangSilent) => Class::HangSilent,
            OwnKill::Monitor(KillOutcome::NoProgress) => Class::NoProgress,
            OwnKill::MaxS => Class::MaxTime,
        });
    }
    if let Some(marker) = f
        .marker
        .filter(|m| *m != Marker::ProviderUnavailable || f.exit != 0)
    {
        return finish(match marker {
            Marker::Quota => Class::Quota,
            Marker::RateLimit => Class::RateLimit,
            Marker::Auth => Class::Auth,
            Marker::ModelUnavailable => Class::ModelUnavailable,
            Marker::ProviderUnavailable => Class::ProviderUnavailable,
        });
    }
    if matches!(f.exit, -9 | 137) {
        return finish(Class::KilledExternal);
    }
    if f.exit == 0 {
        return finish(if usable(f) {
            Class::Ok
        } else if !f.content && !f.tool {
            Class::Empty
        } else {
            match f.role {
                Role::Review if f.tool_call_only => Class::ToolCallOnly,
                Role::Review => Class::NoVerdict,
                Role::Implement => Class::Neutral(Terminal::WriterNoChanges),
                _ => Class::Empty,
            }
        });
    }
    finish(Class::Crash)
}
