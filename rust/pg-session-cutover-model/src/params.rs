/// Seconds per `Tick`.
pub const TICK_SECS: u16 = 10;
/// "At once" for an urgent event, event to a reader (rule 3.48, D-6578): no class may exceed it.
pub const URGENT_BOUND_SECS: u16 = 90;
/// Longest gap between two supervisor passes (the five-minute watchdog is too slow).
pub const SUPERVISOR_SECS: u16 = 20;
/// Longest gap between two reads of one source.
pub const SCAN_SECS: u16 = 60;
pub const MAX_SOURCES: usize = 2;
/// Most events one exploration may define.
pub const MAX_EVENTS: usize = 64;
/// Longest timer, in ticks, that the counters hold without overflow.
pub const MAX_TICKS: u16 = 200;

/// The urgent classes of A3 requirement 1, each with its own time budget.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum UrgentClass {
    GoRow,
    GrantAnswer,
    MainRed,
    RunFailed,
    LeaseMargin,
    RoleBlocked,
}

impl UrgentClass {
    pub const ALL: [UrgentClass; 6] = [
        UrgentClass::GoRow,
        UrgentClass::GrantAnswer,
        UrgentClass::MainRed,
        UrgentClass::RunFailed,
        UrgentClass::LeaseMargin,
        UrgentClass::RoleBlocked,
    ];

    pub(crate) fn index(self) -> usize {
        self as usize
    }
}

/// Ages, in seconds from occurrence, at which each hand-over must have happened: the wake
/// path of today takes over (`takeover`), the operator is told (`alarm`), and the event is
/// held by a reader (`bound`). Each step has one supervisor pass to run.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Budget {
    pub takeover_secs: u16,
    pub alarm_secs: u16,
    pub bound_secs: u16,
}

impl Budget {
    /// 90 s for every class until the A2 shadow latencies and the operator say otherwise.
    pub const DEFAULT: Budget = Budget {
        takeover_secs: 40,
        alarm_secs: 60,
        bound_secs: 90,
    };
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct EventSpec {
    pub class: UrgentClass,
    pub source: u8,
}

/// The bounds of one exploration: the events, their budgets and the supervisor and scan periods.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Params {
    pub budgets: [Budget; 6],
    pub supervisor_secs: u16,
    pub scan_secs: u16,
    pub events: Vec<EventSpec>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParamError {
    /// A time that is zero or not a whole number of ticks.
    Tick,
    /// A time beyond what the counters hold, or a count outside the bounds.
    Range,
    /// A class budget beyond the 90 s ruling.
    OverRuling,
    /// A step does not leave the supervisor a pass before the next deadline.
    SupervisorTooSlow,
}

impl Params {
    pub fn production(events: Vec<EventSpec>) -> Self {
        Self {
            budgets: [Budget::DEFAULT; 6],
            supervisor_secs: SUPERVISOR_SECS,
            scan_secs: SCAN_SECS,
            events,
        }
    }

    /// Budget of one class.
    pub fn budget(&self, class: UrgentClass) -> Budget {
        self.budgets[class.index()]
    }

    pub fn check(&self) -> Result<(), ParamError> {
        let mut secs = vec![self.supervisor_secs, self.scan_secs];
        for b in &self.budgets {
            secs.extend([b.takeover_secs, b.alarm_secs, b.bound_secs]);
        }
        if secs.iter().any(|s| *s == 0 || s % TICK_SECS != 0) {
            return Err(ParamError::Tick);
        }
        if secs.iter().any(|s| s / TICK_SECS > MAX_TICKS)
            || self.events.len() > MAX_EVENTS
            || self
                .events
                .iter()
                .any(|e| usize::from(e.source) >= MAX_SOURCES)
        {
            return Err(ParamError::Range);
        }
        if self
            .budgets
            .iter()
            .any(|b| b.bound_secs > URGENT_BOUND_SECS)
        {
            return Err(ParamError::OverRuling);
        }
        let sup = self.supervisor_secs;
        let paced = |b: &Budget| {
            b.takeover_secs + sup <= b.alarm_secs && b.alarm_secs + sup <= b.bound_secs
        };
        if !self.budgets.iter().all(paced) {
            return Err(ParamError::SupervisorTooSlow);
        }
        Ok(())
    }

    pub(crate) fn ticks(secs: u16) -> u8 {
        u8::try_from(secs / TICK_SECS).unwrap_or(u8::MAX)
    }
}

/// A guard that a named finding showed missing. A defective model keeps the unsafe behaviour,
/// so a test can show that the invariant list catches it.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Defect {
    /// No operator alarm: an unread event can pass its bound.
    NoAlarm,
    /// The supervisor cannot end a session that ignores a pending event: it can read it late.
    NoKill,
    NoSupervisor,
    ActivateWithoutGates,
    /// Activation does not look at an urgent event that is already overdue.
    ActivateOverdue,
}

impl Defect {
    pub const ALL: [Defect; 5] = [
        Defect::NoAlarm,
        Defect::NoKill,
        Defect::NoSupervisor,
        Defect::ActivateWithoutGates,
        Defect::ActivateOverdue,
    ];

    pub(crate) fn bit(self) -> u16 {
        1 << (self as u16)
    }
}
