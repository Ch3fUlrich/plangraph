use crate::UrgentClass;

/// Rollback switch of the role. `Off` is today's behaviour; `Shadow` writes digests only.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Mode {
    Off,
    Shadow,
    Active,
}

/// What must be in place before a role is switched (A3 requirement 7a).
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Gates {
    /// The generation fence, or a verified quiescent handoff in its place.
    pub fence: bool,
    pub recovery: bool,
    /// A live GO for this role (rule 5.9).
    pub go: bool,
    pub shadow_ok: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Life {
    Idle,
    Active,
    Stopped,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Waiter {
    None,
    Armed,
    Fired,
    Dead,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Session {
    pub life: Life,
    pub waiter: Waiter,
    /// A queued task notification that re-invokes an idle session.
    pub notify: bool,
}

impl Session {
    pub fn live(&self) -> bool {
        self.life != Life::Stopped
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Stage {
    Future,
    Occurred,
    /// Read by the collector into a digest (the scan cursor); a pending wake until a receipt.
    Detected,
    /// Held by a reader: the session by its receipt, or the operator by the alarm.
    Received,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Evt {
    pub class: UrgentClass,
    pub source: u8,
    pub stage: Stage,
    pub age: u8,
    /// Also handed to today's wake path (takeover, rollback, or a role not yet switched).
    pub escalated: bool,
    /// The operator was told; the event is closed for the model.
    pub alarmed: bool,
    /// A session's receipt covered it.
    pub seen: bool,
}

impl Evt {
    pub fn open(&self) -> bool {
        matches!(self.stage, Stage::Occurred | Stage::Detected)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Event {
    Tick,
    Occur(u8),
    Scan(u8),
    Hang(u8),
    Heal(u8),
    WaiterFire,
    WaiterDie,
    BeginTurn,
    EndTurn,
    Arm,
    Receive,
    Crash,
    Escalate(u8),
    Alarm(u8),
    Nudge,
    StartShadow,
    SignOffShadow,
    DeployFence,
    DeployRecovery,
    GrantGo,
    RevokeGo,
    Activate,
    Rollback,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Outcome {
    Applied,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Denied {
    Index,
    Mode,
    Gate,
    Guard,
    /// An urgent event is already past its alarm age.
    Overdue,
    /// An obligation is due, so time may not pass.
    Deadline,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Invariant {
    /// Req 7a: an active role has fence, recovery, a live GO and a passed shadow.
    ActivationGate,
    /// Req 1: no urgent event is unread past its class bound; the fallback, the alarm and
    /// a dead role are not exempt.
    UrgentBound,
    /// An event is received only by a session's receipt or by the alarm, never by a scan.
    ReceivedUnread,
    /// Req 4: no active role stays asleep with nothing armed or woken beyond one supervisor pass.
    Unattended,
}
