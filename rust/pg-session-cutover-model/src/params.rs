pub const TICK_SECS: u16 = 10;
/// "At once" (rule 3.48, D-6578): no class may have a bound above it.
pub const URGENT_BOUND_SECS: u16 = 90;
pub const SUPERVISOR_SECS: u16 = 20;
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

/// Ages, in seconds from occurrence, by which the wake path of today takes over, the operator
/// is told and a session has read the event; the supervisor gets one pass between steps.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Budget {
    pub takeover_secs: u16,
    pub alarm_secs: u16,
    pub bound_secs: u16,
}

impl Budget {
    pub const DEFAULT: Budget = Budget {
        takeover_secs: 40,
        alarm_secs: 60,
        bound_secs: 90,
    };
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct EventSpec {
    pub class: UrgentClass,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Params {
    pub budgets: [Budget; 6],
    pub supervisor_secs: u16,
    pub events: Vec<EventSpec>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParamError {
    Tick,
    Range,
    OverRuling,
    /// A step leaves the supervisor less than one pass before the next.
    SupervisorTooSlow,
}

impl Params {
    pub fn production(events: Vec<EventSpec>) -> Self {
        Self {
            budgets: [Budget::DEFAULT; 6],
            supervisor_secs: SUPERVISOR_SECS,
            events,
        }
    }

    pub fn budget(&self, class: UrgentClass) -> Budget {
        self.budgets[class as usize]
    }

    pub fn check(&self) -> Result<(), ParamError> {
        let (b, sup) = (&self.budgets, self.supervisor_secs);
        let mut secs = vec![sup];
        secs.extend(
            b.iter()
                .flat_map(|b| [b.takeover_secs, b.alarm_secs, b.bound_secs]),
        );
        if secs.iter().any(|s| *s == 0 || s % TICK_SECS != 0) {
            return Err(ParamError::Tick);
        }
        if secs.iter().any(|s| s / TICK_SECS > MAX_TICKS) {
            return Err(ParamError::Range);
        }
        if b.iter().any(|b| b.bound_secs > URGENT_BOUND_SECS) {
            return Err(ParamError::OverRuling);
        }
        let paced = |b: &Budget| {
            b.takeover_secs + sup <= b.alarm_secs && b.alarm_secs + sup <= b.bound_secs
        };
        if b.iter().all(paced) {
            Ok(())
        } else {
            Err(ParamError::SupervisorTooSlow)
        }
    }

    pub(crate) fn ticks(secs: u16) -> u8 {
        u8::try_from(secs / TICK_SECS).unwrap_or(u8::MAX)
    }
}

/// A guard a finding showed missing; a test shows an invariant catches its absence.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Defect {
    NoAlarm,
    NoSupervisor,
    NoRecovery,
}

impl Defect {
    pub(crate) fn bit(self) -> u16 {
        1 << (self as u16)
    }
}
