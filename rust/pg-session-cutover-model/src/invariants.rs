use crate::*;

impl Model {
    /// Check the relations that must hold after every event; empty means all hold.
    pub fn violations(&self) -> Vec<Invariant> {
        let mut out = Vec::new();
        let g = self.gates;
        let active = self.mode == Mode::Active;
        if active && !(g.fence && g.recovery && g.go && g.shadow_ok) {
            out.push(Invariant::ActivationGate);
        }
        // The operator is told by the bound, in every mode: the alarm covers a dead role and a
        // hung source. The session's own deadline is a reported miss (`misses`), not a guard.
        let silent = |e: &&Evt| {
            e.binding && e.open() && e.operator_at.is_none() && e.age > self.bound_ticks(e.class)
        };
        if self.events.iter().filter(silent).count() > 0 {
            out.push(Invariant::UrgentBound);
        }
        if self
            .events
            .iter()
            .any(|e| e.stage == Stage::Received && !e.seen)
        {
            out.push(Invariant::ReceivedUnread);
        }
        if active && self.asleep_age > Params::ticks(self.p.supervisor_secs) && self.nudge_target()
        {
            out.push(Invariant::Unattended);
        }
        out
    }

    /// Session deadlines that were missed, by event. The deadline stays attached to the event
    /// across mode changes, an operator alarm and the end of the session; a receipt after the
    /// bound stays visible after it happened.
    pub fn misses(&self) -> Vec<Miss> {
        let mut out = Vec::new();
        for (i, e) in self.events.iter().enumerate() {
            let bound = self.bound_ticks(e.class);
            if !e.binding {
                continue;
            }
            if e.open() && e.age > bound {
                out.push(Miss::Unread(i as u8));
            }
            if e.session_at.is_some_and(|a| a > bound) {
                out.push(Miss::Late(i as u8));
            }
        }
        out
    }

    pub fn invariants_hold(&self) -> bool {
        self.violations().is_empty()
    }
}
