//! Ordinary selection: which ready parts ride, which are left out and why.
use super::rules::{choose, Left, Pool, Rule};
use super::*;

pub(crate) fn overlap(a: &PartSpec, b: &PartSpec) -> bool {
    a.paths & b.paths != 0
}

impl Model {
    pub(crate) fn live_ordinary(&self) -> bool {
        self.trains
            .iter()
            .any(|t| t.kind == Kind::Ordinary && !t.closed)
    }
    pub(crate) fn live_retry(&self) -> bool {
        self.trains
            .iter()
            .any(|t| matches!(t.kind, Kind::Retry { .. }) && !t.closed)
    }

    /// The parent has a verified landing, or rides earlier in this train.
    pub(crate) fn parent_ok(&self, part: PartId, riding: &[PartId]) -> Result<(), PartId> {
        match self.parts[part as usize].spec.parent {
            Some(p) if !riding.contains(&p) && !self.parts[p as usize].verified => Err(p),
            _ => Ok(()),
        }
    }

    fn ready(&self) -> Vec<PartId> {
        self.order
            .iter()
            .copied()
            .filter(|&p| self.parts[p as usize].status == Status::Ready)
            .collect()
    }

    fn due(&self, ready: &[PartId]) -> bool {
        ready.len() >= self.policy.target as usize
            || ready
                .iter()
                .any(|&p| self.parts[p as usize].waited >= self.policy.cut_after)
    }

    fn pool(&self) -> ModelPool<'_> {
        ModelPool(self)
    }

    pub(crate) fn select_ordinary(&mut self) -> Result<Outcome, Denied> {
        if self.live_retry() || !self.groups.is_empty() {
            return Err(Denied::RetryFirst);
        }
        if self.live_ordinary() {
            return Err(Denied::OrdinaryOpen);
        }
        let ready = self.ready();
        if !self.due(&ready) {
            return Err(Denied::NotDue);
        }
        let rule = Rule {
            target: self.policy.target as usize,
            bound: self.policy.bound as u32,
            order: self.policy.order,
        };
        let ids: Vec<usize> = ready.iter().map(|&p| p as usize).collect();
        let (riding, left) = choose(&self.pool(), &ids, rule);
        let riding: Vec<PartId> = riding.into_iter().map(|p| p as PartId).collect();
        let left_out: Vec<(PartId, Disposition)> = left
            .into_iter()
            .map(|(p, l)| {
                let d = match l {
                    Left::Overlap { with, waited } => Disposition::Overlap {
                        with: with as PartId,
                        waited: waited as u8,
                    },
                    Left::ParentNotLanded { parent } => Disposition::ParentNotLanded {
                        parent: parent as PartId,
                    },
                    Left::Target => Disposition::Target,
                };
                (p as PartId, d)
            })
            .collect();
        if riding.is_empty() {
            return Err(Denied::NotDue);
        }
        let id = self.trains.len() as TrainId;
        riding
            .iter()
            .for_each(|&p| self.parts[p as usize].status = Status::InTrain(id));
        self.trains.push(Train {
            kind: Kind::Ordinary,
            included: Some(riding.clone()),
            parts: riding,
            left_out,
            base: self.main,
            bound: false,
            sealed: false,
            frozen: vec![],
            ci: Ci::NotStarted,
            closed: false,
            landed: false,
        });
        Ok(Outcome::Train(id))
    }
}

struct ModelPool<'a>(&'a Model);

impl Pool for ModelPool<'_> {
    fn waited(&self, p: usize) -> u32 {
        self.0.parts[p].waited as u32
    }
    fn overlaps(&self, a: usize, b: usize) -> bool {
        overlap(&self.0.parts[a].spec, &self.0.parts[b].spec)
    }
    fn parent(&self, p: usize) -> Option<usize> {
        self.0.parts[p].spec.parent.map(|q| q as usize)
    }
    fn verified(&self, p: usize) -> bool {
        self.0.parts[p].verified
    }
}
