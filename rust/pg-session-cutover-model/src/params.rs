/// Seconds per `Tick`.
pub const TICK_SECS: u16 = 10;
/// "At once" for an urgent event, event to session receipt (rule 3.48, D-6578).
pub const URGENT_BOUND_SECS: u16 = 90;
/// Age at which the supervisor hands an unreceived urgent event to today's wake path.
pub const TAKEOVER_SECS: u16 = 70;
/// Longest gap between two supervisor passes (the five-minute watchdog is too slow).
pub const SUPERVISOR_SECS: u16 = 20;
/// Longest gap between two reads of one source.
pub const SCAN_SECS: u16 = 60;
/// Consecutive Stop-hook blocks Claude Code 2.1.329 honours before it ends the turn.
pub const STOP_BLOCK_CAP: u8 = 9;
/// Longest digest: the fleet's Bash output limit. The range header counts against it.
pub const DIGEST_BYTES: u16 = 8192;
/// The range header every digest starts with (digest id, first and last event).
pub const HEADER_BYTES: u16 = 64;
/// Longest wait for a launched replacement to be confirmed and adopted, or aborted.
pub const RECYCLE_SECS: u16 = 30;
pub const MAX_SESSIONS: u8 = 3;
pub const MAX_SOURCES: usize = 2;
/// Most events one exploration may define: the occurrence counter is a byte.
pub const MAX_EVENTS: usize = 64;
/// Longest timer, in ticks, that the counters hold without overflow.
pub const MAX_TICKS: u16 = 200;

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
    /// Size of the event's line in a digest.
    pub bytes: u16,
}

/// The bounds of one exploration: sessions, sources, events and direct messages.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct Params {
    /// Sessions the role may use; `sessions - 1` recycles fit.
    pub sessions: u8,
    pub stop_cap: u8,
    /// Bytes of one digest, header included; a receipt covers what fits, urgent first.
    pub digest_bytes: u16,
    pub bound_secs: u16,
    pub takeover_secs: u16,
    pub supervisor_secs: u16,
    pub scan_secs: u16,
    pub recycle_secs: u16,
    pub events: Vec<EventSpec>,
    pub messages: u8,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParamError {
    /// A time that is zero or not a whole number of ticks.
    Tick,
    /// A time beyond what the counters hold, or a count outside the bounds.
    Range,
    SupervisorTooSlow,
}

impl Params {
    pub fn production(events: Vec<EventSpec>, messages: u8) -> Self {
        Self {
            sessions: 2,
            stop_cap: STOP_BLOCK_CAP,
            digest_bytes: DIGEST_BYTES,
            bound_secs: URGENT_BOUND_SECS,
            takeover_secs: TAKEOVER_SECS,
            supervisor_secs: SUPERVISOR_SECS,
            scan_secs: SCAN_SECS,
            recycle_secs: RECYCLE_SECS,
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
            self.recycle_secs,
        ];
        if secs.iter().any(|s| *s == 0 || s % TICK_SECS != 0) {
            return Err(ParamError::Tick);
        }
        if secs.iter().any(|s| s / TICK_SECS > MAX_TICKS)
            || self.sessions == 0
            || self.sessions > MAX_SESSIONS
            || self.events.len() > MAX_EVENTS
            || self
                .events
                .iter()
                .any(|e| e.bytes == 0 || e.bytes > self.digest_bytes.saturating_sub(HEADER_BYTES))
            || self
                .events
                .iter()
                .any(|e| usize::from(e.source) >= MAX_SOURCES)
        {
            return Err(ParamError::Range);
        }
        if self.takeover_secs + self.supervisor_secs > self.bound_secs {
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
    NoTakeover,
    RecycleWhileActive,
    NoFence,
    LaterToolCallHandles,
    CarrySnapshotOnly,
    ActivateWithoutGates,
    NoSupervisor,
    /// The fallback is queued but the session is not held to read it inside the bound.
    FallbackNotAwaited,
    /// A scan counts as the session's receipt.
    ScanIsReceipt,
    /// A digest carries every pending event, whatever its size.
    DigestUnbounded,
    /// The range is written after the event lines, where an output cut drops it.
    FooterRange,
}

impl Defect {
    pub const ALL: [Defect; 11] = [
        Defect::NoTakeover,
        Defect::RecycleWhileActive,
        Defect::NoFence,
        Defect::LaterToolCallHandles,
        Defect::CarrySnapshotOnly,
        Defect::ActivateWithoutGates,
        Defect::NoSupervisor,
        Defect::FallbackNotAwaited,
        Defect::ScanIsReceipt,
        Defect::DigestUnbounded,
        Defect::FooterRange,
    ];

    pub(crate) fn bit(self) -> u16 {
        1 << (self as u16)
    }
}
