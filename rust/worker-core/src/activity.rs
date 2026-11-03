use crate::hosts::Role;
use serde::Deserialize;

#[cfg(test)]
mod tests;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Request,
    Content,
    Tool,
    Verdict,
    Error,
    Weak,
}
impl Kind {
    pub fn is_strong(self) -> bool {
        matches!(self, Self::Content | Self::Tool | Self::Verdict)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ToolClass {
    Read,
    Exec,
    Edit,
    Other,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Start,
    End,
    Done,
}
#[derive(Clone, Copy, Debug)]
pub struct Signal {
    pub kind: Kind,
    pub ts_ms: u64,
    pub tool: Option<(ToolClass, Phase)>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Off,
    Shadow,
    On,
}
#[derive(Clone, Copy, Debug)]
pub struct Modes {
    pub first: Mode,
    pub silent: Mode,
    pub milestone: Mode,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rule {
    FirstActivity,
    Silent,
    Milestone,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Act {
    Warn,
    Kill,
    WouldWarn,
    WouldKill,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KillOutcome {
    HangNoActivity,
    HangSilent,
    NoProgress,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Decision {
    pub rule: Rule,
    pub act: Act,
    pub outcome: KillOutcome,
    pub open_tools: u32,
    pub s_eff_s: Option<u64>,
}
#[derive(Clone, Copy, Debug, Default)]
pub struct Ctx {
    pub edited: bool,
    pub safe_alt: bool,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Pending,
    Active,
    Ended(Rule),
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
pub struct Limits {
    pub first_activity_s: Option<u64>,
    pub silence_s: Option<u64>,
    pub milestone_s: Option<u64>,
    pub tool_max_s: Option<u64>,
    pub confirm_s: Option<u64>,
    pub cpu_active_ms: Option<u64>,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            first_activity_s: Some(180),
            silence_s: Some(600),
            milestone_s: Some(600),
            tool_max_s: Some(1800),
            confirm_s: Some(3),
            cpu_active_ms: Some(1500),
        }
    }
}

pub struct Monitor {
    pub(crate) _role: Role,
    limits: Limits,
    modes: Modes,
    t0_ms: u64,
    state: State,
    first_activity_ms: Option<u64>,
    last_signal_ms: Option<u64>,
    last_strong_ms: Option<u64>,
    open_tools: u32,
    strong_since_request: bool,
    first_reported: bool,
    silent_reported: bool,
}
impl Monitor {
    pub fn new(role: Role, limits: &Limits, modes: Modes, t0_ms: u64) -> Self {
        Self {
            _role: role,
            limits: *limits,
            modes,
            t0_ms,
            state: State::Pending,
            first_activity_ms: None,
            last_signal_ms: None,
            last_strong_ms: None,
            open_tools: 0,
            strong_since_request: false,
            first_reported: false,
            silent_reported: false,
        }
    }
    pub fn state(&self) -> State {
        self.state
    }
    pub fn first_activity_ms(&self) -> Option<u64> {
        self.first_activity_ms
    }
    pub fn last_signal_ms(&self) -> Option<u64> {
        self.last_signal_ms
    }
    pub fn last_strong_ms(&self) -> Option<u64> {
        self.last_strong_ms
    }
    pub fn open_tools(&self) -> u32 {
        self.open_tools
    }
    pub fn s_eff_s(&self) -> Option<u64> {
        self.limits.silence_s?;
        if self.open_tools > 0 {
            self.limits.tool_max_s
        } else {
            self.limits.silence_s
        }
    }
    fn refresh(&mut self, ts_ms: u64) {
        let next = Some(self.last_signal_ms.map_or(ts_ms, |old| old.max(ts_ms)));
        if next != self.last_signal_ms {
            self.silent_reported = false;
        }
        self.last_signal_ms = next;
    }
    pub fn feed(&mut self, s: Signal) {
        if matches!(self.state, State::Ended(_)) || s.kind == Kind::Error {
            return;
        }
        let strong = s.kind.is_strong();
        if self.state == State::Pending {
            if !strong && s.kind != Kind::Request {
                return;
            }
            self.state = State::Active;
            self.first_activity_ms = Some(s.ts_ms);
            self.refresh(s.ts_ms);
        }
        if strong {
            self.refresh(s.ts_ms);
            self.last_strong_ms = Some(self.last_strong_ms.map_or(s.ts_ms, |old| old.max(s.ts_ms)));
            self.strong_since_request = true;
            if s.kind == Kind::Tool {
                match s.tool.map(|(_, phase)| phase) {
                    Some(Phase::Start) => self.open_tools = self.open_tools.saturating_add(1),
                    Some(Phase::End) => self.open_tools = self.open_tools.saturating_sub(1),
                    _ => {}
                }
            }
        } else if s.kind == Kind::Weak {
            self.refresh(s.ts_ms);
        } else if s.kind == Kind::Request {
            if self.strong_since_request {
                self.refresh(s.ts_ms);
            }
            self.strong_since_request = false;
        }
    }
    pub fn tick(&mut self, now_ms: u64, _ctx: Ctx) -> Option<Decision> {
        let (rule, mode, start, limit, reported, outcome) = match self.state {
            State::Pending => (
                Rule::FirstActivity,
                self.modes.first,
                self.t0_ms,
                self.limits.first_activity_s,
                &mut self.first_reported,
                KillOutcome::HangNoActivity,
            ),
            State::Active => (
                Rule::Silent,
                self.modes.silent,
                self.last_signal_ms?,
                self.s_eff_s(),
                &mut self.silent_reported,
                KillOutcome::HangSilent,
            ),
            State::Ended(_) => return None,
        };
        let threshold = limit?
            .saturating_add(self.limits.confirm_s.unwrap_or(0))
            .saturating_mul(1000);
        if mode == Mode::Off || *reported || now_ms.saturating_sub(start) < threshold {
            return None;
        }
        *reported = true;
        let act = if mode == Mode::On {
            self.state = State::Ended(rule);
            Act::Kill
        } else {
            Act::WouldKill
        };
        Some(Decision {
            rule,
            act,
            outcome,
            open_tools: self.open_tools,
            s_eff_s: self.s_eff_s(),
        })
    }
}
