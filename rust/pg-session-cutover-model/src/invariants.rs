use crate::*;

impl Model {
    /// Check the relations that must hold after every event; empty means all hold.
    pub fn violations(&self) -> Vec<Invariant> {
        let mut out = Vec::new();
        let bound = |e: &Evt| self.at(e, |b| b.bound_secs);
        // The operator is told by the bound, in every mode; a dead role and a working session
        // are not exempt.
        if self
            .pending()
            .any(|e| e.age >= bound(e) && e.operator_at.is_none())
        {
            out.push(Invariant::UrgentBound);
        }
        // A session has read it by the bound, whatever it is doing; the alarm does not stand in.
        if self.pending().any(|e| e.age > bound(e)) {
            out.push(Invariant::SessionBound);
        }
        if self.asleep_age > Params::ticks(self.p.supervisor_secs) && self.unattended() {
            out.push(Invariant::Unattended);
        }
        out
    }
}
