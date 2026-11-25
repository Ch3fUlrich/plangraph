use crate::UrgentClass;

/// `Off` is today's behaviour; `Active` is the digest wait.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Mode {
    Off,
    Active,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Life {
    Idle,
    Active,
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Session {
    pub life: Life,
    pub notify: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Stage {
    Future,
    Occurred,
    Received,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Evt {
    pub class: UrgentClass,
    pub stage: Stage,
    pub age: u8,
    pub escalated: bool,
    /// Held to its deadline in every mode: it occurred while active or was pending at `Activate`.
    pub binding: bool,
    /// Age, in ticks, of the session's receipt and of the operator's alarm. An alarm does not
    /// close the event.
    pub session_at: Option<u8>,
    pub operator_at: Option<u8>,
}

impl Evt {
    pub fn open(&self) -> bool {
        self.stage == Stage::Occurred
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Event {
    Tick,
    Occur(u8),
    WakeLost,
    BeginTurn,
    EndTurn,
    Receive,
    Crash,
    Escalate(u8),
    Alarm(u8),
    Nudge,
    /// The authorized recovery of an idle or dead session; its successor reads what it receives.
    Recover,
    GrantGo,
    Activate,
    Rollback,
    RevokeGo,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Denied {
    Guard,
    Deadline,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Invariant {
    UrgentBound,
    /// A session has read every binding event by its bound, whatever it is doing; the alarm
    /// does not stand in for it.
    SessionBound,
    /// An owed supervisor retry is never idle for more than one pass.
    Unattended,
}
