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
    /// Acknowledged by the session's receipt. An alarm does not close an event.
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
    /// The operator was told. The event stays pending until a session receives it.
    pub alarmed: bool,
    /// A session's receipt covered it.
    pub seen: bool,
    /// Its deadline holds in every mode: it occurred while active, or the role was switched on
    /// with it pending. Rollback and a lost GO do not release it.
    pub binding: bool,
    /// Age, in ticks, at which the session received it. Kept as evidence of a late receipt.
    pub session_at: Option<u8>,
    /// Age, in ticks, at which the operator was told. Tracked apart from the session's receipt:
    /// the alarm never stands in for it.
    pub operator_at: Option<u8>,
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
    /// The queued wake is dropped before the session begins a turn.
    WakeLost,
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
    /// Req 1: the operator is told of a binding urgent event by its class bound, in any mode
    /// (or a session has received it); a dead role and a hung source are not exempt.
    UrgentBound,
    /// An event is received only by a session's receipt, never by a scan or an alarm.
    ReceivedUnread,
    /// Req 4: no active role stays asleep with nothing armed or woken beyond one supervisor pass.
    Unattended,
}

/// A session's deadline that was missed. It is reported, not prevented: a receipt timeout does
/// not stop a working session (that takes the authorized recovery sequence of the next slice).
/// The miss stays attached to the event whatever happens to the session or the operator alarm.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Miss {
    /// No session has received the event and it is past its class bound.
    Unread(u8),
    /// A session received the event after its class bound.
    Late(u8),
}
