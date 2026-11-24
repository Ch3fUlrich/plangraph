use crate::*;
use std::sync::Arc;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Model {
    pub(crate) p: Arc<Params>,
    pub(crate) defects: u16,
    pub(crate) mode: Mode,
    pub(crate) gates: Gates,
    pub(crate) sessions: Vec<Session>,
    pub(crate) adopted: u8,
    pub(crate) recycle: Recycle,
    pub(crate) events: Vec<Evt>,
    pub(crate) msgs: Vec<Msg>,
    pub(crate) hung: [bool; MAX_SOURCES],
    pub(crate) blind: [bool; MAX_SOURCES],
    pub(crate) since_scan: [u8; MAX_SOURCES],
    pub(crate) asleep_age: u8,
    pub(crate) adopted_over_active: bool,
    pub(crate) recycle_age: u8,
    pub(crate) occurred: u8,
    pub(crate) last_digest: Option<Digest>,
}

/// One section of a digest, in the order it is written.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Part {
    /// Occurrence order of the first and last event the digest covers.
    Range(u8, u8),
    /// An event line, by its occurrence order.
    Event(u8),
}

/// The last digest receipt: its sections in output order, and its size, header included.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Digest {
    pub parts: Vec<Part>,
    pub bytes: u32,
}

impl Digest {
    pub fn events(&self) -> usize {
        self.parts
            .iter()
            .filter(|p| matches!(p, Part::Event(_)))
            .count()
    }

    pub fn range(&self) -> Option<(u8, u8)> {
        self.parts.iter().find_map(|p| match p {
            Part::Range(a, b) => Some((*a, *b)),
            Part::Event(_) => None,
        })
    }
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
                order: 0,
                bytes: e.bytes,
                escalated: false,
                seen: false,
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
            recycle_age: 0,
            occurred: 0,
            last_digest: None,
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

    pub fn event_count(&self) -> u8 {
        self.events.len() as u8
    }

    pub fn message_count(&self) -> u8 {
        self.msgs.len() as u8
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

    pub fn hung(&self, source: u8) -> bool {
        self.hung[usize::from(source)]
    }

    pub fn blind(&self, source: u8) -> bool {
        self.blind[usize::from(source)]
    }

    pub fn asleep_secs(&self) -> u16 {
        u16::from(self.asleep_age) * TICK_SECS
    }

    pub fn last_digest(&self) -> Option<&Digest> {
        self.last_digest.as_ref()
    }

    pub fn live_current(&self) -> usize {
        self.sessions
            .iter()
            .filter(|s| s.live() && !s.fenced)
            .count()
    }

    /// Events scanned into a digest and not yet received: what the next receipt covers.
    pub fn unreceived(&self) -> usize {
        self.events
            .iter()
            .filter(|e| e.stage == Stage::Detected)
            .count()
    }

    /// Every event the bounds allow, for an exhaustive search.
    pub fn alphabet(&self) -> Vec<Event> {
        let mut out = vec![Event::Tick];
        for s in 0..MAX_SOURCES as u8 {
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
}
