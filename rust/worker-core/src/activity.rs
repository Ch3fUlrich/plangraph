use crate::hosts::Role;
use serde::Deserialize;

#[cfg(test)]
mod milestone_tests;
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
    pub kill_at_ms: Option<u64>,
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
    first_warned: bool,
    silent_warned: bool,
    milestone: bool,
    milestone_warned: bool,
    milestone_reported: bool,
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
            first_warned: false,
            silent_warned: false,
            milestone: false,
            milestone_warned: false,
            milestone_reported: false,
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
            self.silent_warned = false;
        }
        self.last_signal_ms = next;
    }
    pub fn feed(&mut self, s: Signal) {
        if matches!(self.state, State::Ended(_)) || s.kind == Kind::Error {
            return;
        }
        let strong = s.kind.is_strong();
        self.milestone |= s.kind == Kind::Verdict
            || (s.kind == Kind::Tool
                && matches!(s.tool, Some((ToolClass::Edit, Phase::End | Phase::Done))));
        if self.state == State::Pending {
            if !strong && s.kind != Kind::Request {
                return;
            }
            self.state = State::Active;
            self.first_activity_ms = Some(s.ts_ms.max(self.t0_ms));
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
    pub fn tick(&mut self, now_ms: u64, ctx: Ctx) -> Option<Decision> {
        self.milestone |= ctx.edited;
        let m_limit = self.limits.milestone_s;
        let m_age = now_ms.saturating_sub(self.first_activity_ms.unwrap_or(now_ms));
        let m_enabled = self.state == State::Active
            && !self.milestone
            && (!self.milestone_reported || !self.milestone_warned)
            && self.modes.milestone != Mode::Off;
        let m_due = m_enabled
            && !self.milestone_reported
            && m_limit.is_some_and(|s| m_age >= s.saturating_mul(1000));
        // Latch safety at the first M deadline, even if S wins this tick.
        if m_due && !ctx.safe_alt {
            self.milestone_reported = true;
        }
        let s_kill_due = self.modes.silent != Mode::Off
            && self.silent_warned
            && !self.silent_reported
            && self.s_eff_s().is_some_and(|s| {
                now_ms.saturating_sub(self.last_signal_ms.unwrap_or(now_ms))
                    >= s.saturating_add(self.limits.confirm_s.unwrap_or(0))
                        .saturating_mul(1000)
            });
        // Kills precede warnings; S wins ties. Do not consume an undelivered S warn.
        let m_kill_first = m_due && ctx.safe_alt && self.milestone_warned && !s_kill_due;
        if !m_kill_first {
            if let Some(d) = self.tick_activity(now_ms) {
                return Some(d);
            }
        }
        if !m_enabled || m_limit.is_none_or(|s| m_age < s.saturating_mul(600)) {
            return None;
        }
        let act = if !self.milestone_warned {
            self.milestone_warned = true;
            if self.modes.milestone == Mode::On {
                Act::Warn
            } else {
                Act::WouldWarn
            }
        } else if m_due && ctx.safe_alt {
            self.milestone_reported = true;
            if self.modes.milestone == Mode::On {
                self.state = State::Ended(Rule::Milestone);
                Act::Kill
            } else {
                Act::WouldKill
            }
        } else {
            return None;
        };
        Some(Decision {
            rule: Rule::Milestone,
            act,
            outcome: KillOutcome::NoProgress,
            open_tools: self.open_tools,
            s_eff_s: self.s_eff_s(),
            kill_at_ms: Some(
                self.first_activity_ms?
                    .saturating_add(m_limit?.saturating_mul(1000)),
            ),
        })
    }
    fn tick_activity(&mut self, now_ms: u64) -> Option<Decision> {
        let (rule, mode, start, limit, reported, warned, outcome) = match self.state {
            State::Pending => (
                Rule::FirstActivity,
                self.modes.first,
                self.t0_ms,
                self.limits.first_activity_s,
                &mut self.first_reported,
                &mut self.first_warned,
                KillOutcome::HangNoActivity,
            ),
            State::Active => (
                Rule::Silent,
                self.modes.silent,
                self.last_signal_ms?,
                self.s_eff_s(),
                &mut self.silent_reported,
                &mut self.silent_warned,
                KillOutcome::HangSilent,
            ),
            State::Ended(_) => return None,
        };
        let limit = limit?;
        let threshold = limit
            .saturating_add(self.limits.confirm_s.unwrap_or(0))
            .saturating_mul(1000);
        let age = now_ms.saturating_sub(start);
        if mode == Mode::Off || *reported || age < limit.saturating_mul(800) {
            return None;
        }
        let act = if !*warned {
            *warned = true;
            if mode == Mode::On {
                Act::Warn
            } else {
                Act::WouldWarn
            }
        } else {
            if age < threshold {
                return None;
            }
            *reported = true;
            if mode == Mode::On {
                self.state = State::Ended(rule);
                Act::Kill
            } else {
                Act::WouldKill
            }
        };
        Some(Decision {
            rule,
            act,
            outcome,
            open_tools: self.open_tools,
            s_eff_s: self.s_eff_s(),
            kill_at_ms: Some(start.saturating_add(threshold)),
        })
    }
}
