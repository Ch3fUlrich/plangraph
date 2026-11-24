//! The verifier of recorded dispositions: each of Overlap, ParentNotLanded and Target is accepted
//! only when the facts it names hold.
use super::select::overlap;
use super::*;

impl Model {
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
                }
                Disposition::ParentNotLanded { parent } => {
                    p.spec.parent == Some(parent)
                        && !t.parts.contains(&parent)
                        && !self.parts[parent as usize].verified
                }
                Disposition::Target => t.parts.len() >= self.policy.target as usize,
            };
        if ok {
            Ok(Outcome::Accepted)
        } else {
            Err(Denied::Disposition)
        }
    }
}
