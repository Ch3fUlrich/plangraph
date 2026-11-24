use crate::*;

impl Model {
    /// Check the relations that must hold after every event; empty means all hold.
    pub fn violations(&self) -> Vec<Invariant> {
        let mut out = Vec::new();
        let g = self.gates;
        let gated = g.fence != Fence::Absent && g.recovery && g.go && g.shadow_ok;
        let active = self.mode == Mode::Active;
        if active && !gated {
            out.push(Invariant::ActivationGate);
        }
        if self.live_current() > 1 || self.sessions[usize::from(self.adopted)].fenced {
            out.push(Invariant::SingleCurrent);
        }
        if self
            .sessions
            .iter()
            .filter(|s| s.live() && s.waiter == Waiter::Armed)
            .count()
            > 1
        {
            out.push(Invariant::TwoArmed);
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
        // The bound runs to the session's receipt, on the fallback path too. With no live
        // current session the recovery path owns the event (`Unattended` bounds that).
        let bound = Params::ticks(self.p.bound_secs);
        let late = |e: &&Evt| {
            e.class == Class::Urgent
                && matches!(e.stage, Stage::Occurred | Stage::Detected)
                && e.age > bound
        };
        if active && self.live_current() > 0 && self.events.iter().filter(late).count() > 0 {
            out.push(Invariant::UrgentBound);
        }
        if self
            .events
            .iter()
            .any(|e| e.stage == Stage::Received && !e.seen)
        {
            out.push(Invariant::ReceivedUnread);
        }
        if self
            .last_digest
            .is_some_and(|d| d.bytes > self.p.digest_bytes)
        {
            out.push(Invariant::DigestOverBudget);
        }
        let sup = Params::ticks(self.p.supervisor_secs);
        if active && self.asleep_age > sup && self.supervisor_can_act() {
            out.push(Invariant::Unattended);
        }
        if self.adopted_over_active {
            out.push(Invariant::AdoptedOverActive);
        }
        if active && self.sessions.iter().any(|s| s.live() && s.cron) {
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
            // Only an active role launches or confirms; rollback and a lost GO abort these.
            Recycle::Launching { pred, succ } | Recycle::Confirmed { pred, succ } => {
                self.mode == Mode::Active
                    && pred != succ
                    && succ < n
                    && pred == self.adopted
                    && self.sessions[usize::from(succ)].fenced
            }
            Recycle::StopPending { pred, succ } => pred != succ && succ < n && succ == self.adopted,
        }
    }
}
