//! The relations that must hold after every event.
use super::*;

/// The red rounds the lineage of a retry group had before it.
fn lineage_round(t: &Train) -> u8 {
    match t.kind {
        Kind::Retry { round, .. } => round,
        _ => 0,
    }
}

impl Model {
    pub fn invariants_hold(&self) -> bool {
        self.membership_holds()
            && self.trains_hold()
            && self.speculation_holds()
            && self.dependencies_hold()
            && self.retry_bounded()
    }

    /// A part is in at most one live train, and the train it names holds it.
    fn membership_holds(&self) -> bool {
        self.parts.iter().enumerate().all(|(i, p)| {
            let id = i as PartId;
            let live: Vec<usize> = (0..self.trains.len())
                .filter(|&t| !self.trains[t].closed && self.trains[t].parts.contains(&id))
                .collect();
            match p.status {
                Status::InTrain(t) => live == [t as usize],
                _ => live.is_empty(),
            }
        })
    }

    fn live_count(&self, f: impl Fn(&Kind) -> bool) -> usize {
        self.trains
            .iter()
            .filter(|t| !t.closed && f(&t.kind))
            .count()
    }

    fn trains_hold(&self) -> bool {
        let ordinary = self.live_count(|k| *k == Kind::Ordinary);
        let retry = self.live_count(|k| matches!(k, Kind::Retry { .. }));
        let spec = self.live_count(|k| matches!(k, Kind::Speculative { .. }));
        ordinary <= 1
            && retry <= 1
            && spec <= 1
            // Retry precedence: nothing else is open while retry work is pending or running.
            && (ordinary + spec == 0
                || (retry == 0 && self.groups.is_empty())
                || self.retry_beside())
            && self.trains.iter().all(|t| {
                (!t.sealed || t.parts == t.frozen)
                    && (t.kind != Kind::Ordinary || t.parts.len() <= self.policy.target as usize)
                    && t.parts.len() <= self.policy.cap as usize
                    && (!t.landed || t.ci == Ci::Green && t.bound && t.checked && t.tested == Some(t.tree))
                    && (t.ci != Ci::Running || t.sealed && t.bound)
                    // Fixing in place ends with the second red round.
                    && (t.closed || t.ci != Ci::Red || t.red_rounds + lineage_round(t) < 2)
                    // The stale bound (D-6840): tested fast-forward never rebuilds a retry train a third time,
                    // and only such a train lands stale.
                    && (self.policy.stale_k == 0
                        || self.policy.landing != Landing::TestedFf
                        || !matches!(t.kind, Kind::Retry { .. })
                        || t.rebuilds <= self.policy.stale_k)
                    && (!t.stale_landed
                        || matches!(t.kind, Kind::Retry { .. })
                            && self.policy.landing == Landing::TestedFf
                            && t.rebuilds >= self.policy.stale_k)
            })
            && self.trains.iter().filter(|t| t.landed).count() <= self.main as usize
    }

    /// A live candidate sits on its predecessor: main's tip once the predecessor has landed, or the
    /// commit after it while the predecessor is still tested; anything else was discarded.
    fn speculation_holds(&self) -> bool {
        self.trains
            .iter()
            .filter(|t| !t.closed)
            .all(|t| match t.kind {
                Kind::Speculative { pred } => {
                    let p = &self.trains[pred as usize];
                    let pred_ok = p.kind == Kind::Ordinary && (!p.closed || p.landed);
                    pred_ok && t.sealed && t.base == self.main + u8::from(!p.landed)
                }
                _ => true,
            })
    }

    /// A landed part has a landed parent; a part in a train or a held group has its parent in the same
    /// set or landed, so no train ever carries a child without its parent.
    fn dependencies_hold(&self) -> bool {
        let landed = |q: PartId| self.parts[q as usize].status == Status::Landed;
        let closed = |set: &[PartId]| {
            set.iter().all(|&p| {
                self.parts[p as usize]
                    .spec
                    .parent
                    .is_none_or(|q| set.contains(&q) || landed(q))
            })
        };
        self.parts
            .iter()
            .all(|p| p.status != Status::Landed || p.spec.parent.is_none_or(landed))
            && self
                .trains
                .iter()
                .filter(|t| !t.closed)
                .all(|t| closed(&t.parts))
            && self.groups.iter().enumerate().all(|(i, g)| {
                // A later half may lean on a part that lands first: an earlier group or the running train.
                let ahead: Vec<PartId> = self
                    .trains
                    .iter()
                    .filter(|t| !t.closed)
                    .flat_map(|t| t.parts.iter().copied())
                    .chain(
                        self.groups[..i]
                            .iter()
                            .flat_map(|g| g.parts.iter().copied()),
                    )
                    .collect();
                g.parts.iter().all(|&p| {
                    self.parts[p as usize].status == Status::Held
                        && self.parts[p as usize]
                            .spec
                            .parent
                            .is_none_or(|q| g.parts.contains(&q) || ahead.contains(&q) || landed(q))
                })
            })
    }

    /// Retry runs of a lineage stay within the halving tree of its red train: 2n - 2.
    fn retry_bounded(&self) -> bool {
        self.runs
            .iter()
            .all(|&(_, runs, n)| runs as usize <= (2 * n as usize).saturating_sub(2))
    }
}
