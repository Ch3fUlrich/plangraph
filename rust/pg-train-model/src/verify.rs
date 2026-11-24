//! The verifier of recorded dispositions: each is accepted only when the facts it names hold, and a
//! part that was ready with no place and no reason is refused.
use super::select::overlap;
use super::*;

impl Model {
    /// A part that has waited its bound, or (policy (a)) been deferred often enough, rides despite an
    /// overlap: an Overlap reason is only true while the part is not due. The count already holds the
    /// deferral this reason records.
    fn due_now(&self, p: &Part) -> bool {
        let policy = &self.policy;
        p.waited >= policy.bound || (policy.order == Order::Count && p.deferrals > policy.due_after)
    }

    fn descends_from(&self, mut part: PartId, of: PartId) -> bool {
        while let Some(q) = self.parts[part as usize].spec.parent {
            if q == of {
                return true;
            }
            part = q;
        }
        false
    }

    pub(crate) fn verify_left_out(&self, train: TrainId, part: PartId) -> Result<Outcome, Denied> {
        let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
        let d = t
            .left_out
            .iter()
            .find(|l| l.0 == part)
            .ok_or(Denied::Phase)?
            .1;
        let p = &self.parts[part as usize];
        let ok = !t.parts.contains(&part)
            && match d {
                Disposition::Overlap { with, waited } => {
                    with != part
                        && t.parts.contains(&with)
                        && overlap(&p.spec, &self.parts[with as usize].spec)
                        && waited < self.policy.bound
                        && !self.due_now(p)
                }
                Disposition::ParentNotLanded { parent } => {
                    p.spec.parent == Some(parent)
                        && !t.parts.contains(&parent)
                        && !self.parts[parent as usize].verified
                }
                Disposition::Target => t.parts.len() >= self.policy.target as usize,
                Disposition::Cap => t.parts.len() >= self.policy.cap as usize,
                Disposition::Conflict => t.conflicted.contains(&part),
                Disposition::Dropped => p.status == Status::Dropped,
                Disposition::Descendant { of } => {
                    of != part && !t.parts.contains(&of) && self.descends_from(part, of)
                }
            };
        if ok {
            Ok(Outcome::Accepted)
        } else {
            Err(Denied::Disposition)
        }
    }

    /// Every recorded reason holds, and every part that was ready when the train was cut rides, has a
    /// reason, or has moved on.
    pub(crate) fn verify_train(&self, train: TrainId) -> Result<Outcome, Denied> {
        let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
        for &(part, _) in &t.left_out {
            self.verify_left_out(train, part)?;
        }
        let unaccounted = t.ready_at_cut.iter().any(|&p| {
            self.parts[p as usize].status == Status::Ready
                && !t.parts.contains(&p)
                && !t.left_out.iter().any(|l| l.0 == p)
        });
        if unaccounted {
            Err(Denied::Disposition)
        } else {
            Ok(Outcome::Accepted)
        }
    }

    pub(crate) fn verify_complete(&mut self, train: TrainId) -> Result<Outcome, Denied> {
        let out = self.verify_train(train)?;
        self.trains[train as usize].checked = true;
        Ok(out)
    }
}
