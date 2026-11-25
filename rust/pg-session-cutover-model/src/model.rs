use crate::*;
use std::sync::Arc;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    pub(crate) p: Arc<Params>,
    pub(crate) defects: u16,
    pub(crate) mode: Mode,
    /// A live GO for the role (rule 5.9). The fence and recovery of A3 requirement 7a are deployed.
    pub(crate) go: bool,
    pub(crate) session: Session,
    pub(crate) events: Vec<Evt>,
    pub(crate) asleep_age: u8,
}

impl Model {
    pub fn new(params: Params) -> Result<Self, ParamError> {
        params.check()?;
        let evt = |e: &EventSpec| Evt {
            class: e.class,
            stage: Stage::Future,
            age: 0,
            escalated: false,
            binding: false,
            session_at: None,
            operator_at: None,
        };
        let events = params.events.iter().map(evt).collect();
        let session = Session {
            life: Life::Idle,
            notify: false,
        };
        Ok(Self {
            p: Arc::new(params),
            defects: 0,
            mode: Mode::Off,
            go: false,
            session,
            events,
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

    pub fn life(&self) -> Life {
        self.session.life
    }

    pub fn stage(&self, event: u8) -> Stage {
        self.events[usize::from(event)].stage
    }

    pub fn age_secs(&self, event: u8) -> u16 {
        u16::from(self.events[usize::from(event)].age) * TICK_SECS
    }

    pub fn session_secs(&self, event: u8) -> Option<u16> {
        self.events[usize::from(event)]
            .session_at
            .map(|a| u16::from(a) * TICK_SECS)
    }

    pub fn operator_secs(&self, event: u8) -> Option<u16> {
        self.events[usize::from(event)]
            .operator_at
            .map(|a| u16::from(a) * TICK_SECS)
    }
}
