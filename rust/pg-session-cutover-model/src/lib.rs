//! Pure model of one orchestrator role switching from today's waits to the digest wait.
//! Each successful event is one atomic transition; a refused event changes nothing.
//! Time is a `Tick` of `TICK_SECS`; a tick is refused while an obligation is due, so an
//! obligation (scan, wake, takeover, supervision) cannot be skipped by letting time pass.
mod transition;

use std::sync::Arc;

pub const TICK_SECS: u16 = 10;
/// "At once" for an urgent event, event to wake handed over (rule 3.48, D-6578).
pub const URGENT_BOUND_SECS: u16 = 90;
/// Age at which the supervisor hands an unreceived urgent event to today's wake path.
pub const TAKEOVER_SECS: u16 = 70;
/// Longest gap between two supervisor passes (the five-minute watchdog is too slow).
pub const SUPERVISOR_SECS: u16 = 20;
/// Longest gap between two reads of one source.
pub const SCAN_SECS: u16 = 60;
/// Consecutive Stop-hook blocks Claude Code 2.1.329 honours before it ends the turn.
pub const STOP_BLOCK_CAP: u8 = 9;
pub const MAX_SESSIONS: u8 = 3;
pub const MAX_SOURCES: usize = 2;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Class {
    Urgent,
    Ordinary,
    /// Router heartbeat and build-L1 beat: explicit classes that follow-ups do not replace.
    Beat,
    FollowUp,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct EventSpec {
    pub class: Class,
    pub source: u8,
}

/// The bounds of one exploration: sessions, sources, events and direct messages.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Params {
    /// Sessions the role may use; `sessions - 1` recycles fit.
    pub sessions: u8,
    pub stop_cap: u8,
    pub bound_secs: u16,
    pub takeover_secs: u16,
    pub supervisor_secs: u16,
    pub scan_secs: u16,
    pub events: Vec<EventSpec>,
    pub messages: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParamError {
    Tick,
    Bounds,
    SupervisorTooSlow,
}

impl Params {
    pub fn production(events: Vec<EventSpec>, messages: u8) -> Self {
        Self {
            sessions: 2,
            stop_cap: STOP_BLOCK_CAP,
            bound_secs: URGENT_BOUND_SECS,
            takeover_secs: TAKEOVER_SECS,
            supervisor_secs: SUPERVISOR_SECS,
            scan_secs: SCAN_SECS,
            events,
            messages,
        }
    }

    /// The takeover happens at most one supervisor pass after the takeover age, so that sum
    /// must fit the bound.
    pub fn check(&self) -> Result<(), ParamError> {
        let secs = [
            self.bound_secs,
            self.takeover_secs,
            self.supervisor_secs,
            self.scan_secs,
        ];
        if secs.iter().any(|s| *s == 0 || s % TICK_SECS != 0) {
            return Err(ParamError::Tick);
        }
        if self.sessions == 0
            || self.sessions > MAX_SESSIONS
            || self
                .events
                .iter()
                .any(|e| usize::from(e.source) >= MAX_SOURCES)
        {
            return Err(ParamError::Bounds);
        }
        if self.takeover_secs + self.supervisor_secs > self.bound_secs {
            return Err(ParamError::SupervisorTooSlow);
        }
        Ok(())
    }

    fn ticks(secs: u16) -> u8 {
        u8::try_from(secs / TICK_SECS).unwrap_or(u8::MAX)
    }
}

/// A guard that a named finding showed missing. A defective model keeps the unsafe behaviour,
/// so a test can show that the invariant list catches it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Defect {
    NoTakeover,
    RecycleWhileActive,
    NoFence,
    LaterToolCallHandles,
    CarrySnapshotOnly,
    ActivateWithoutGates,
    NoSupervisor,
}

impl Defect {
    pub const ALL: [Defect; 7] = [
        Defect::NoTakeover,
        Defect::RecycleWhileActive,
        Defect::NoFence,
        Defect::LaterToolCallHandles,
        Defect::CarrySnapshotOnly,
        Defect::ActivateWithoutGates,
        Defect::NoSupervisor,
    ];

    fn bit(self) -> u8 {
        1 << (self as u8)
    }
}

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
struct Session {
    life: Life,
    waiter: Waiter,
    /// A queued task notification or direct message that re-invokes an idle session.
    notify: bool,
    /// Not the keeper's current generation: its receipts, handling and waiter are refused.
    fenced: bool,
    /// Handoff and card written; cleared when a new turn starts, which makes them stale.
    ready: bool,
    cron: bool,
    blocks: u8,
}

impl Session {
    fn new(life: Life, fenced: bool) -> Self {
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

    fn live(&self) -> bool {
        matches!(self.life, Life::Idle | Life::Active)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Stage {
    Future,
    Occurred,
    /// Read by the collector into a digest; a pending wake until the session's receipt.
    Detected,
    Received,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct Evt {
    class: Class,
    source: u8,
    stage: Stage,
    age: u8,
    /// Also handed to today's wake path (takeover, rollback, or a role not yet switched).
    escalated: bool,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Home {
    Future,
    /// The role's durable inbox, read by the next session that is adopted.
    Inbox,
    Session(u8),
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct Msg {
    home: Home,
    read: bool,
    /// The effect was done. The record is the stable-id handling record in the keeper.
    acted: bool,
    record: bool,
    /// Present at the forward snapshot taken when the recycle began.
    snap: bool,
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
    FencedWaiterExit(u8),
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
    RecycleShape,
    /// Req 3: no unhandled direct message sits on a stopped, fenced session.
    MessageStranded,
    /// Req 3: a handling record always follows the effect.
    HandledWithoutAction,
    /// Req 1: an unreceived urgent event is handed to today's wake path inside the bound.
    UrgentBound,
    /// Req 4: no active role stays asleep with nothing armed or woken beyond one supervisor pass.
    Unattended,
    /// Req 2: no recycle took over a working session.
    AdoptedOverActive,
    /// Req 5: no session cron in an active role.
    CronInActive,
    /// Req 4: the Stop hook never exceeds its continuation cap.
    StopCap,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    p: Arc<Params>,
    defects: u8,
    mode: Mode,
    gates: Gates,
    sessions: Vec<Session>,
    adopted: u8,
    recycle: Recycle,
    events: Vec<Evt>,
    msgs: Vec<Msg>,
    hung: [bool; MAX_SOURCES],
    blind: [bool; MAX_SOURCES],
    since_scan: [u8; MAX_SOURCES],
    asleep_age: u8,
    adopted_over_active: bool,
}

impl Model {
    /// Start with session 0 live and current, the role switched off and nothing happened yet.
    pub fn new(params: Params) -> Result<Self, ParamError> {
        params.check()?;
        let mut sessions = vec![Session::new(Life::Idle, false)];
        sessions.resize(
            usize::from(params.sessions),
            Session::new(Life::Unlaunched, true),
        );
        let events = params
            .events
            .iter()
            .map(|e| Evt {
                class: e.class,
                source: e.source,
                stage: Stage::Future,
                age: 0,
                escalated: false,
            })
            .collect();
        let msgs = vec![
            Msg {
                home: Home::Future,
                read: false,
                acted: false,
                record: false,
                snap: false
            };
            usize::from(params.messages)
        ];
        Ok(Self {
            p: Arc::new(params),
            defects: 0,
            mode: Mode::Off,
            gates: Gates {
                fence: Fence::Absent,
                recovery: false,
                go: false,
                shadow_ok: false,
            },
            sessions,
            adopted: 0,
            recycle: Recycle::Idle,
            events,
            msgs,
            hung: [false; MAX_SOURCES],
            blind: [false; MAX_SOURCES],
            since_scan: [0; MAX_SOURCES],
            asleep_age: 0,
            adopted_over_active: false,
        })
    }

    pub fn with_defect(mut self, defect: Defect) -> Self {
        self.defects |= defect.bit();
        self
    }

    fn has(&self, defect: Defect) -> bool {
        self.defects & defect.bit() != 0
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    pub fn gates(&self) -> Gates {
        self.gates
    }

    pub fn recycle(&self) -> Recycle {
        self.recycle
    }

    pub fn adopted(&self) -> u8 {
        self.adopted
    }

    pub fn life(&self, session: u8) -> Life {
        self.sessions[usize::from(session)].life
    }

    pub fn waiter(&self, session: u8) -> Waiter {
        self.sessions[usize::from(session)].waiter
    }

    pub fn fenced(&self, session: u8) -> bool {
        self.sessions[usize::from(session)].fenced
    }

    pub fn notified(&self, session: u8) -> bool {
        self.sessions[usize::from(session)].notify
    }

    pub fn stage(&self, event: u8) -> Stage {
        self.events[usize::from(event)].stage
    }

    pub fn escalated(&self, event: u8) -> bool {
        self.events[usize::from(event)].escalated
    }

    /// Age of an event in seconds, saturated one tick above the bound.
    pub fn age_secs(&self, event: u8) -> u16 {
        u16::from(self.events[usize::from(event)].age) * TICK_SECS
    }

    pub fn home(&self, msg: u8) -> Home {
        self.msgs[usize::from(msg)].home
    }

    pub fn handled(&self, msg: u8) -> bool {
        self.msgs[usize::from(msg)].record
    }

    pub fn event_count(&self) -> u8 {
        self.events.len() as u8
    }

    pub fn message_count(&self) -> u8 {
        self.msgs.len() as u8
    }

    pub fn hung(&self, source: u8) -> bool {
        self.hung[usize::from(source)]
    }

    pub fn blind(&self, source: u8) -> bool {
        self.blind[usize::from(source)]
    }

    pub fn asleep_secs(&self) -> u16 {
        u16::from(self.asleep_age) * TICK_SECS
    }

    pub fn live_current(&self) -> usize {
        self.sessions
            .iter()
            .filter(|s| s.live() && !s.fenced)
            .count()
    }

    /// Every event the bounds allow, for an exhaustive search.
    pub fn alphabet(&self) -> Vec<Event> {
        let mut out = vec![Event::Tick];
        let sources = 0..MAX_SOURCES as u8;
        for s in sources {
            out.extend([Event::Scan(s), Event::Hang(s), Event::Heal(s)]);
        }
        for e in 0..self.p.events.len() as u8 {
            out.extend([Event::Occur(e), Event::Escalate(e)]);
        }
        for i in 0..self.p.sessions {
            out.extend([
                Event::WaiterFire(i),
                Event::WindowEnd(i),
                Event::WaiterDie(i),
                Event::FencedWaiterExit(i),
                Event::BeginTurn(i),
                Event::Arm(i),
                Event::Receive(i),
                Event::EndTurn(i),
                Event::ToolUse(i),
                Event::ArmCron(i),
                Event::ClearCron(i),
                Event::Crash(i),
                Event::MarkReady(i),
            ]);
            for m in 0..self.p.messages {
                out.extend([
                    Event::ReadMsg { msg: m, session: i },
                    Event::HandleMsg { msg: m, session: i },
                ]);
            }
        }
        out.extend((0..self.p.messages).map(Event::SendMsg));
        out.extend([
            Event::Nudge,
            Event::BeginRecycle,
            Event::Confirm,
            Event::AbortRecycle,
            Event::Adopt,
            Event::StopOk,
            Event::StopFail,
            Event::StartShadow,
            Event::SignOffShadow,
            Event::DeployFence(Fence::Deployed),
            Event::DeployFence(Fence::QuiescentHandoff),
            Event::DeployRecovery,
            Event::GrantGo,
            Event::RevokeGo,
            Event::Activate,
            Event::Rollback,
        ]);
        out
    }

    /// Check the relations that must hold after every event; empty means all hold.
    pub fn violations(&self) -> Vec<Invariant> {
        let mut out = Vec::new();
        let g = self.gates;
        let gated = g.fence != Fence::Absent && g.recovery && g.go && g.shadow_ok;
        if self.mode == Mode::Active && !gated {
            out.push(Invariant::ActivationGate);
        }
        if self.live_current() > 1 || self.sessions[usize::from(self.adopted)].fenced {
            out.push(Invariant::SingleCurrent);
        }
        if !self.recycle_shape_ok() {
            out.push(Invariant::RecycleShape);
        }
        let stranded = |m: &Msg| match m.home {
            Home::Session(i) => {
                let s = &self.sessions[usize::from(i)];
                s.life == Life::Stopped && s.fenced && !m.record
            }
            _ => false,
        };
        if self.msgs.iter().any(stranded) {
            out.push(Invariant::MessageStranded);
        }
        if self.msgs.iter().any(|m| m.record && !m.acted) {
            out.push(Invariant::HandledWithoutAction);
        }
        let bound = Params::ticks(self.p.bound_secs);
        let open = |e: &&Evt| {
            e.class == Class::Urgent && matches!(e.stage, Stage::Occurred | Stage::Detected)
        };
        if self.mode == Mode::Active
            && self
                .events
                .iter()
                .filter(open)
                .any(|e| e.age > bound && !e.escalated)
        {
            out.push(Invariant::UrgentBound);
        }
        if self.mode == Mode::Active
            && self.asleep_age > Params::ticks(self.p.supervisor_secs)
            && self.supervisor_can_act()
        {
            out.push(Invariant::Unattended);
        }
        if self.adopted_over_active {
            out.push(Invariant::AdoptedOverActive);
        }
        if self.mode == Mode::Active && self.sessions.iter().any(|s| s.live() && s.cron) {
            out.push(Invariant::CronInActive);
        }
        if self.sessions.iter().any(|s| s.blocks > self.p.stop_cap) {
            out.push(Invariant::StopCap);
        }
        out
    }

    pub fn invariants_hold(&self) -> bool {
        self.violations().is_empty()
    }

    fn recycle_shape_ok(&self) -> bool {
        let n = self.p.sessions;
        match self.recycle {
            Recycle::Idle => true,
            Recycle::Launching { pred, succ } | Recycle::Confirmed { pred, succ } => {
                pred != succ
                    && succ < n
                    && pred == self.adopted
                    && self.sessions[usize::from(succ)].fenced
            }
            Recycle::StopPending { pred, succ } => pred != succ && succ < n && succ == self.adopted,
        }
    }
}
