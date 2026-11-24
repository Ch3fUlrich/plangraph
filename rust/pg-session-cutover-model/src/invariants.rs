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
        // The bound runs from occurrence to a first reader, in every mode and whatever the
        // session does: a woken session that keeps working, a hung source or a dead role are
        // handed to the operator by the alarm, which leaves the event pending for the session.
        let unheld = |e: &&Evt| {
            e.binding && e.open() && e.reader.is_none() && e.age > self.bound_ticks(e.class)
        };
        if self.events.iter().filter(unheld).count() > 0 {
            out.push(Invariant::UrgentBound);
        }
        let late = |e: &&Evt| e.binding && e.reader.is_some_and(|a| a > self.bound_ticks(e.class));
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
