//! Cutting a train from the ready parts: which ride, which are left out and why.
use super::rules::{choose, Left, Pool, Rule};
use super::*;

pub(crate) fn overlap(a: &PartSpec, b: &PartSpec) -> bool {
    a.paths & b.paths != 0
}

pub(crate) struct ModelPool<'a>(pub(crate) &'a Model);

impl Pool for ModelPool<'_> {
    fn waited(&self, p: usize) -> u32 {
        self.0.parts[p].waited as u32
    }
    fn deferrals(&self, p: usize) -> u32 {
        self.0.parts[p].deferrals as u32
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

impl Model {
    fn live(&self, f: impl Fn(&Kind) -> bool) -> bool {
        self.trains.iter().any(|t| !t.closed && f(&t.kind))
    }
    pub(crate) fn live_ordinary(&self) -> bool {
        self.live(|k| *k == Kind::Ordinary)
    }
    pub(crate) fn live_retry(&self) -> bool {
        self.live(|k| matches!(k, Kind::Retry { .. }))
    }
    pub(crate) fn live_spec(&self) -> bool {
        self.live(|k| matches!(k, Kind::Speculative { .. }))
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

    pub(crate) fn rule(&self) -> Rule {
        let p = &self.policy;
        Rule {
            target: p.target as usize,
            cap: p.cap as usize,
            bound: p.bound as u32,
            due_after: p.due_after as u32,
            order: p.order,
        }
    }

    fn due(&self, ready: &[PartId]) -> bool {
        ready.len() >= self.policy.target as usize
            || ready
                .iter()
                .any(|&p| self.parts[p as usize].waited >= self.policy.cut_after)
    }

    /// Cut a train of `kind` from the ready parts; every ready part gets its place or its reason.
    pub(crate) fn cut(&mut self, kind: Kind, base: u8) -> Result<TrainId, Denied> {
        let ready = self.ready();
        if !self.due(&ready) {
            return Err(Denied::NotDue);
        }
        let rule = self.rule();
        let ids: Vec<usize> = ready.iter().map(|&p| p as usize).collect();
        let (riding, left) = choose(&ModelPool(self), &ids, rule);
        if riding.is_empty() {
            return Err(Denied::NotDue);
        }
        let id = self.trains.len() as TrainId;
        let riding: Vec<PartId> = riding.into_iter().map(|p| p as PartId).collect();
        let mut train = Train::new(kind, riding.clone(), base);
        train.ready_at_cut = ready;
        for (p, l) in left {
            let d = match l {
                Left::Overlap { with, waited } => Disposition::Overlap {
                    with: with as PartId,
                    waited: waited as u8,
                },
                Left::ParentNotLanded { parent } => Disposition::ParentNotLanded {
                    parent: parent as PartId,
                },
                Left::Target => Disposition::Target,
                Left::Cap => Disposition::Cap,
            };
            if matches!(d, Disposition::Overlap { .. } | Disposition::Target) {
                let n = &mut self.parts[p].deferrals;
                *n = n.saturating_add(1);
            }
            train.left_out.push((p as PartId, d));
        }
        self.release(&riding, Status::InTrain(id));
        self.trains.push(train);
        Ok(id)
    }

    pub(crate) fn select_ordinary(&mut self) -> Result<Outcome, Denied> {
        if self.live_retry() || !self.groups.is_empty() {
            return Err(Denied::RetryFirst);
        }
        if self.live_ordinary() || self.live_spec() {
            return Err(Denied::OrdinaryOpen);
        }
        let base = self.main;
        self.cut(Kind::Ordinary, base).map(Outcome::Train)
    }
}
