//! The speculative next train: built on the unlanded train and tested meanwhile, promoted only in
//! tested fast-forward when main's tip is exactly the predecessor commit it was built on.
use super::*;

impl Model {
    /// Throw away every live speculative candidate built on `pred` (or on anything, if `None`):
    /// its parts are ready again.
    pub(crate) fn discard_specs(&mut self, pred: Option<TrainId>) {
        for i in 0..self.trains.len() {
            let t = &self.trains[i];
            let hit =
                matches!(t.kind, Kind::Speculative { pred: p } if pred.is_none_or(|q| q == p));
            if hit && !t.closed {
                let parts = t.parts.clone();
                self.trains[i].closed = true;
                self.release(&parts, Status::Ready);
            }
        }
    }

    /// Main moved by anything but the landing of the predecessor: no candidate is on main's tip any more.
    pub(crate) fn bump_main(&mut self) {
        self.main += 1;
        self.discard_specs(None);
    }

    pub(crate) fn speculate(&mut self, pred: TrainId) -> Result<Outcome, Denied> {
        let p = self.trains.get(pred as usize).ok_or(Denied::Phase)?;
        if p.closed || p.kind != Kind::Ordinary || !p.sealed || p.ci == Ci::Red {
            return Err(Denied::Phase);
        }
        if self.live_spec() {
            return Err(Denied::OrdinaryOpen);
        }
        if self.live_retry() || !self.groups.is_empty() {
            return Err(Denied::RetryFirst);
        }
        // After `pred` lands, main is at the version one past the one `pred` was built on.
        let base = self.main + 1;
        let before = self.clone();
        let id = self.cut(Kind::Speculative { pred }, base)?;
        // A candidate is built only on a complete, verified membership; a refusal changes nothing.
        if let Err(d) = self.verify_train(id) {
            *self = before;
            return Err(d);
        }
        self.trains[id as usize] = self.trains[id as usize].clone().started();
        Ok(Outcome::Train(id))
    }

    pub(crate) fn promote(&mut self, train: TrainId) -> Result<Outcome, Denied> {
        let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
        let Kind::Speculative { pred } = t.kind else {
            return Err(Denied::Phase);
        };
        if t.closed {
            return Err(Denied::Phase);
        }
        if self.policy.landing != Landing::TestedFf {
            return Err(Denied::Mode);
        }
        if !self.trains[pred as usize].landed {
            return Err(Denied::Phase);
        }
        if t.base != self.main {
            self.discard_specs(Some(pred));
            return Ok(Outcome::Discarded);
        }
        if t.ci != Ci::Green {
            return Err(Denied::NotGreen);
        }
        if t.tested != Some(t.tree) {
            return Err(Denied::TreeChanged);
        }
        // Promotion needs the same current verification as a settlement.
        if !t.checked {
            return Err(Denied::Unverified);
        }
        self.land(train, true);
        Ok(Outcome::Applied)
    }
}
