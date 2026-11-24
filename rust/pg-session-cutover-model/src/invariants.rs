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
        // Two deadlines, tracked apart. The operator is told by the bound, in every mode (the
        // alarm covers a dead role and a hung source). A live session has read it by the bound,
        // or the supervisor has ended it; a late receipt stays visible after it happened.
        let past = |e: &Evt| e.binding && e.age > self.bound_ticks(e.class);
        let silent = |e: &&Evt| past(e) && e.open() && e.operator_at.is_none();
        if self.events.iter().filter(silent).count() > 0 {
            out.push(Invariant::UrgentBound);
        }
        let live = self.session.live();
        if live && self.events.iter().any(|e| past(e) && e.open()) {
            out.push(Invariant::SessionUnread);
        }
        let late =
            |e: &&Evt| e.binding && e.session_at.is_some_and(|a| a > self.bound_ticks(e.class));
        if self.events.iter().filter(late).count() > 0 {
            out.push(Invariant::LateReceipt);
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

    pub fn invariants_hold(&self) -> bool {
        self.violations().is_empty()
    }
}
