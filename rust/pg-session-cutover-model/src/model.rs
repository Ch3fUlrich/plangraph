use crate::*;
use std::sync::Arc;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    pub(crate) p: Arc<Params>,
    pub(crate) defects: u16,
    pub(crate) mode: Mode,
    pub(crate) gates: Gates,
    pub(crate) session: Session,
    pub(crate) events: Vec<Evt>,
    pub(crate) hung: [bool; MAX_SOURCES],
    pub(crate) blind: [bool; MAX_SOURCES],
    pub(crate) since_scan: [u8; MAX_SOURCES],
    pub(crate) asleep_age: u8,
}

impl Model {
    /// Start with the session live and idle, the role switched off and nothing happened yet.
    pub fn new(params: Params) -> Result<Self, ParamError> {
        params.check()?;
        let events = params
            .events
            .iter()
            .map(|e| Evt {
                class: e.class,
                source: e.source,
                stage: Stage::Future,
                age: 0,
                escalated: false,
                alarmed: false,
                seen: false,
                binding: false,
                session_at: None,
                operator_at: None,
                excused: false,
            })
            .collect();
        Ok(Self {
            p: Arc::new(params),
            defects: 0,
            mode: Mode::Off,
            gates: Gates {
                fence: false,
                recovery: false,
                go: false,
                shadow_ok: false,
            },
            session: Session {
                life: Life::Idle,
                waiter: Waiter::None,
                notify: false,
            },
            events,
            hung: [false; MAX_SOURCES],
            blind: [false; MAX_SOURCES],
            since_scan: [0; MAX_SOURCES],
            asleep_age: 0,
        })
    }

    pub fn with_defect(mut self, defect: Defect) -> Self {
        self.defects |= defect.bit();
        self
    }

    pub(crate) fn has(&self, defect: Defect) -> bool {
        self.defects & defect.bit() != 0
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    pub fn gates(&self) -> Gates {
        self.gates
    }

    pub fn life(&self) -> Life {
        self.session.life
    }

    pub fn waiter(&self) -> Waiter {
        self.session.waiter
    }

    pub fn notified(&self) -> bool {
        self.session.notify
    }

    pub fn stage(&self, event: u8) -> Stage {
        self.events[usize::from(event)].stage
    }

    pub fn escalated(&self, event: u8) -> bool {
        self.events[usize::from(event)].escalated
    }

    /// The operator was told about the event; it stays pending for the session.
    pub fn alarmed(&self, event: u8) -> bool {
        self.events[usize::from(event)].alarmed
    }

    /// Age in seconds at which a session received the event, if one has.
    pub fn session_secs(&self, event: u8) -> Option<u16> {
        self.events[usize::from(event)]
            .session_at
            .map(|a| u16::from(a) * TICK_SECS)
    }

    /// Age in seconds at which the operator was told, if they have been.
    pub fn operator_secs(&self, event: u8) -> Option<u16> {
        self.events[usize::from(event)]
            .operator_at
            .map(|a| u16::from(a) * TICK_SECS)
    }

    pub fn event_count(&self) -> u8 {
        self.events.len() as u8
    }

    /// Age of an event in seconds, saturated at the counter's limit.
    pub fn age_secs(&self, event: u8) -> u16 {
        u16::from(self.events[usize::from(event)].age) * TICK_SECS
    }

    pub fn hung(&self, source: u8) -> bool {
        self.hung[usize::from(source)]
    }

    pub fn blind(&self, source: u8) -> bool {
        self.blind[usize::from(source)]
    }

    /// Every event the bounds allow, for an exhaustive search.
    pub fn alphabet(&self) -> Vec<Event> {
        let mut out = vec![Event::Tick];
        for s in 0..MAX_SOURCES as u8 {
            out.extend([Event::Scan(s), Event::Hang(s), Event::Heal(s)]);
        }
        for e in 0..self.p.events.len() as u8 {
            out.extend([Event::Occur(e), Event::Escalate(e), Event::Alarm(e)]);
        }
        out.extend([
            Event::WaiterFire,
            Event::WaiterDie,
            Event::WakeLost,
            Event::BeginTurn,
            Event::EndTurn,
            Event::Arm,
            Event::Receive,
            Event::Crash,
            Event::Nudge,
            Event::Recover,
            Event::StartShadow,
            Event::SignOffShadow,
            Event::DeployFence,
            Event::DeployRecovery,
            Event::GrantGo,
            Event::RevokeGo,
            Event::Activate,
            Event::Rollback,
        ]);
        out
    }
}
