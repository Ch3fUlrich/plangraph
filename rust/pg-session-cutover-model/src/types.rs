/// Rollback switch of the role. `Off` is today's behaviour; `Shadow` writes digests only.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Mode {
    Off,
    Shadow,
    Active,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Fence {
    Absent,
    /// Keeper-issued generation with one compare-and-set adoption before every stop.
    Deployed,
    /// A verified quiescent handoff stands in for the fence: no automatic recycle.
    QuiescentHandoff,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Gates {
    pub fence: Fence,
    pub recovery: bool,
    pub go: bool,
    pub shadow_ok: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Life {
    Unlaunched,
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
    /// A queued task notification or direct message that re-invokes an idle session.
    pub notify: bool,
    /// Not the keeper's current generation: its receipts, handling and waiter are refused.
    pub fenced: bool,
    /// Handoff and card written; cleared when a new turn starts, which makes them stale.
    pub ready: bool,
    pub cron: bool,
    pub blocks: u8,
}

impl Session {
    pub fn new(life: Life, fenced: bool) -> Self {
        Self {
            life,
            waiter: Waiter::None,
            notify: false,
            fenced,
            ready: false,
            cron: false,
            blocks: 0,
        }
    }

    pub fn live(&self) -> bool {
        matches!(self.life, Life::Idle | Life::Active)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Stage {
    Future,
    Occurred,
    /// Read by the collector into a digest (the scan cursor); a pending wake until a receipt.
    Detected,
    /// Acknowledged by a session's receipt (the ack cursor).
    Received,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Evt {
    pub class: crate::Class,
    pub source: u8,
    pub stage: Stage,
    pub age: u8,
    /// Also handed to today's wake path (takeover, rollback, or a role not yet switched).
    pub escalated: bool,
    /// A session's receipt covered it; only a receipt sets this.
    pub seen: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Home {
    Future,
    /// The role's durable inbox, read by the next session that is adopted.
    Inbox,
    Session(u8),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) struct Msg {
    pub home: Home,
    pub read: bool,
    /// The effect was done. The record is the stable-id handling record in the keeper.
    pub acted: bool,
    pub record: bool,
    /// Present at the forward snapshot taken when the recycle began.
    pub snap: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Recycle {
    Idle,
    /// Ready marker and handoff/card pair accepted, replacement launched, not yet confirmed.
    Launching {
        pred: u8,
        succ: u8,
    },
    Confirmed {
        pred: u8,
        succ: u8,
    },
    /// The replacement is adopted; the predecessor is fenced and its stop is pending.
    StopPending {
        pred: u8,
        succ: u8,
    },
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Event {
    Tick,
    Occur(u8),
    Scan(u8),
    Hang(u8),
    Heal(u8),
    WaiterFire(u8),
    WindowEnd(u8),
    WaiterDie(u8),
    BeginTurn(u8),
    Arm(u8),
    Receive(u8),
    EndTurn(u8),
    ToolUse(u8),
    ArmCron(u8),
    ClearCron(u8),
    Crash(u8),
    MarkReady(u8),
    SendMsg(u8),
    ReadMsg { msg: u8, session: u8 },
    HandleMsg { msg: u8, session: u8 },
    Escalate(u8),
    Nudge,
    BeginRecycle,
    Confirm,
    AbortRecycle,
    Adopt,
    StopOk,
    StopFail,
    StartShadow,
    SignOffShadow,
    DeployFence(Fence),
    DeployRecovery,
    GrantGo,
    RevokeGo,
    Activate,
    Rollback,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Outcome {
    Applied,
    /// The Stop hook kept the turn open.
    Blocked,
    StopFailed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Denied {
    Index,
    Mode,
    Gate,
    Guard,
    Fence,
    /// The session is working, or its handoff is stale.
    Active,
    Cron,
    Bound,
    /// An obligation is due, so time may not pass.
    Deadline,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Invariant {
    /// Req 7a: an active role has fence, recovery, a live GO and a passed shadow.
    ActivationGate,
    /// Req 2: at most one live session is the current generation.
    SingleCurrent,
    /// Req 2: at most one live session holds an armed wait.
    TwoArmed,
    /// Req 2, 7: a recycle in flight has valid, distinct sessions and only an active role launches.
    RecycleShape,
    /// Req 3: no unhandled direct message sits on a stopped, fenced session.
    MessageStranded,
    /// Req 3: a handling record always follows the effect.
    HandledWithoutAction,
    /// Req 1: with a live current session, no urgent event stays unreceived past the bound.
    UrgentBound,
    /// Req 6: an event is received only by a session's receipt, never by a scan.
    ReceivedUnread,
    /// Req 4: no active role stays asleep with nothing armed or woken beyond one supervisor pass.
    Unattended,
    /// Req 2: no recycle took over a working session.
    AdoptedOverActive,
    /// Req 5: no session cron in an active role.
    CronInActive,
    /// Req 4: the Stop hook never exceeds its continuation cap.
    StopCap,
}
