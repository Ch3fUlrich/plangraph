//! A red train is cut into retry groups (rule 3.52 (8)): at stack boundaries, or at a dependency-closed
//! prefix when it is one stack. Retry groups keep their own size and run before ordinary selection (D-6102).
use super::*;

/// Parts in train order, grouped by the root of their stack inside the set.
fn stacks(m: &Model, parts: &[PartId]) -> Vec<Vec<PartId>> {
    let root = |mut p: PartId| {
        while let Some(q) = m.parts[p as usize]
            .spec
            .parent
            .filter(|q| parts.contains(q))
        {
            p = q;
        }
        p
    };
    let mut out: Vec<(PartId, Vec<PartId>)> = Vec::new();
    for &p in parts {
        let r = root(p);
        match out.iter_mut().find(|s| s.0 == r) {
            Some(s) => s.1.push(p),
            None => out.push((r, vec![p])),
        }
    }
    out.into_iter().map(|s| s.1).collect()
}

/// Two non-empty halves; the first holds at least half of the parts and ends on a stack boundary.
pub(crate) fn halve(m: &Model, parts: &[PartId]) -> Vec<Vec<PartId>> {
    if parts.len() < 2 {
        return Vec::new();
    }
    let half = parts.len().div_ceil(2);
    let stacks = stacks(m, parts);
    if stacks.len() < 2 {
        return vec![parts[..half].to_vec(), parts[half..].to_vec()];
    }
    let (mut first, mut count) = (0, 0);
    while first < stacks.len() - 1 && count < half {
        count += stacks[first].len();
        first += 1;
    }
    vec![stacks[..first].concat(), stacks[first..].concat()]
}

impl Model {
    /// A train's CI went red. A named culprit drops with its descendants and the rest retry as one group
    /// (the failing part drops, the rest land); with no culprit the train is halved. A single part that
    /// is red drops. Two red rounds, of the train or of its lineage, escalate it, once.
    pub(crate) fn red(&mut self, train: TrainId, culprit: Option<PartId>) -> Outcome {
        let t = &mut self.trains[train as usize];
        if t.ci == Ci::Running {
            t.red_rounds += 1;
        }
        t.ci = Ci::Red;
        t.closed = true;
        let (parts, kind, rounds) = (t.parts.clone(), t.kind, t.red_rounds);
        let (lineage, round) = match kind {
            Kind::Retry { lineage, round } => (lineage, round),
            _ => {
                self.lineages += 1;
                self.runs.push((self.lineages, 0, parts.len() as u8));
                (self.lineages, 0)
            }
        };
        self.discard_specs(Some(train));
        let groups = match culprit {
            Some(c) => {
                let out = self.with_descendants(c, &parts);
                self.release(&out, Status::Dropped);
                let rest: Vec<PartId> =
                    parts.iter().copied().filter(|p| !out.contains(p)).collect();
                if rest.is_empty() {
                    vec![]
                } else {
                    vec![rest]
                }
            }
            None if parts.len() < 2 => {
                self.release(&parts, Status::Dropped);
                vec![]
            }
            None => halve(self, &parts),
        };
        let count = groups.len();
        for parts in &groups {
            self.release(parts, Status::Held);
        }
        let next = groups.into_iter().map(|parts| Group {
            lineage,
            round: round + 1,
            parts,
        });
        self.groups.splice(0..0, next.collect::<Vec<_>>());
        self.cascade_drops();
        let escalated = rounds + round >= 2 && self.escalated.insert(lineage);
        Outcome::Red {
            groups: count,
            escalated,
        }
    }

    pub(crate) fn start_retry(&mut self) -> Result<Outcome, Denied> {
        if self.live_retry() || self.live_ordinary() || self.live_spec() || self.groups.is_empty() {
            return Err(Denied::Phase);
        }
        // A held child waits for its parent's verified outcome: a legacy settlement verifies nothing.
        let waiting = self.groups[0].parts.iter().any(|&p| {
            self.parts[p as usize].spec.parent.is_some_and(|q| {
                !self.groups[0].parts.contains(&q) && !self.parts[q as usize].verified
            })
        });
        if waiting {
            return Err(Denied::Dependency);
        }
        let g = self.groups.remove(0);
        let id = self.trains.len() as TrainId;
        self.release(&g.parts, Status::InTrain(id));
        if let Some(r) = self.runs.iter_mut().find(|r| r.0 == g.lineage) {
            r.1 += 1;
        }
        let kind = Kind::Retry {
            lineage: g.lineage,
            round: g.round,
        };
        self.trains
            .push(Train::new(kind, g.parts, self.main).started());
        Ok(Outcome::Train(id))
    }
}
