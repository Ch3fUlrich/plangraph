//! What happens to single parts: registration, conflict resolution, drop, revert and re-admission.
use super::*;

impl Model {
    pub(crate) fn release(&mut self, parts: &[PartId], status: Status) {
        parts
            .iter()
            .for_each(|&p| self.parts[p as usize].status = status);
    }

    /// `part` and every part above it in `set`, at any depth.
    pub(crate) fn with_descendants(&self, part: PartId, set: &[PartId]) -> Vec<PartId> {
        let mut out = vec![part];
        loop {
            let more = set.iter().copied().find(|&p| {
                !out.contains(&p)
                    && self.parts[p as usize]
                        .spec
                        .parent
                        .is_some_and(|q| out.contains(&q))
            });
            match more {
                Some(p) => out.push(p),
                None => return out,
            }
        }
    }

    /// A parent that can still land: landed, riding or held.
    fn available(&self, q: PartId) -> bool {
        matches!(
            self.parts[q as usize].status,
            Status::Landed | Status::InTrain(_) | Status::Held
        )
    }

    /// A part waiting in a retry group whose parent cannot land any more, at any depth, goes back to ready.
    pub(crate) fn cascade_drops(&mut self) {
        loop {
            let orphan = self
                .groups
                .iter()
                .flat_map(|g| g.parts.iter().copied())
                .find(|&p| {
                    let part = &self.parts[p as usize];
                    part.status != Status::Held
                        || part.spec.parent.is_some_and(|q| !self.available(q))
                });
            let Some(p) = orphan else { return };
            for g in &mut self.groups {
                g.parts.retain(|&q| q != p);
            }
            self.groups.retain(|g| !g.parts.is_empty());
            if self.parts[p as usize].status == Status::Held {
                self.parts[p as usize].status = Status::Ready;
            }
        }
    }

    pub(crate) fn register(&mut self, part: PartId) -> Result<Outcome, Denied> {
        let p = self.parts.get(part as usize).ok_or(Denied::Phase)?;
        if p.status != Status::Unregistered {
            return Err(Denied::Phase);
        }
        if let Some(q) = p.spec.parent {
            if !self.order.contains(&q) {
                return Err(Denied::Phase);
            }
            // The child of a reverted part is refused at registration.
            if self.parts[q as usize].reverted {
                return Err(Denied::Dependency);
            }
        }
        self.parts[part as usize].status = Status::Ready;
        self.order.push(part);
        Ok(Outcome::Applied)
    }

    /// Take `leaving` out of an open train; each goes back to ready with its reason recorded.
    fn leave_train(
        &mut self,
        train: TrainId,
        leaving: &[PartId],
        root: PartId,
        root_reason: Disposition,
    ) {
        let t = &mut self.trains[train as usize];
        t.parts.retain(|p| !leaving.contains(p));
        t.included = Some(t.parts.clone());
        t.bound = false;
        t.closed = t.parts.is_empty();
        for &p in leaving {
            if !t.ready_at_cut.contains(&p) {
                t.ready_at_cut.push(p);
            }
            t.left_out.retain(|l| l.0 != p);
            let reason = if p == root {
                root_reason
            } else {
                Disposition::Descendant { of: root }
            };
            t.left_out.push((p, reason));
        }
    }

    pub(crate) fn conflict(&mut self, train: TrainId, part: PartId) -> Result<Outcome, Denied> {
        let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
        if t.closed || t.sealed || !t.parts.contains(&part) {
            return Err(Denied::Phase);
        }
        let out = self.with_descendants(part, &t.parts);
        self.trains[train as usize].conflicted.push(part);
        self.leave_train(train, &out, part, Disposition::Conflict);
        self.release(&out, Status::Ready);
        Ok(Outcome::Applied)
    }

    pub(crate) fn drop_part(&mut self, part: PartId) -> Result<Outcome, Denied> {
        let p = self.parts.get(part as usize).ok_or(Denied::Phase)?;
        // The part leaves; what rode or waited on it goes back to ready, to wait for a landed parent.
        match p.status {
            Status::Ready => {}
            Status::Held => {
                let g = self.groups.iter().find(|g| g.parts.contains(&part));
                let leaving = self.with_descendants(part, g.map_or(&[], |g| &g.parts));
                for g in &mut self.groups {
                    g.parts.retain(|q| !leaving.contains(q));
                }
                self.groups.retain(|g| !g.parts.is_empty());
                self.release(&leaving, Status::Ready);
            }
            Status::InTrain(t) if !self.trains[t as usize].sealed => {
                let leaving = self.with_descendants(part, &self.trains[t as usize].parts);
                self.leave_train(t, &leaving, part, Disposition::Conflict);
                self.release(&leaving, Status::Ready);
            }
            _ => return Err(Denied::Phase),
        }
        self.parts[part as usize].status = Status::Dropped;
        self.cascade_drops();
        Ok(Outcome::Dropped)
    }

    pub(crate) fn revert(&mut self, part: PartId) -> Result<Outcome, Denied> {
        if self.parts.get(part as usize).map(|p| p.status) != Some(Status::Landed) {
            return Err(Denied::Phase);
        }
        let all: Vec<PartId> = (0..self.parts.len() as PartId).collect();
        let family = self.with_descendants(part, &all);
        // A child in a running train waits for that train; one in an open train leaves it.
        let sealed = |p: &PartId| match self.parts[*p as usize].status {
            Status::InTrain(t) => self.trains[t as usize].sealed,
            _ => false,
        };
        if family.iter().any(sealed) {
            return Err(Denied::Phase);
        }
        for &p in &family {
            if let Status::InTrain(t) = self.parts[p as usize].status {
                self.leave_train(t, &[p], p, Disposition::Descendant { of: part });
            }
        }
        for p in family {
            let s = self.parts[p as usize].status;
            self.parts[p as usize].verified = false;
            self.parts[p as usize].status = match s {
                Status::Landed => Status::Dropped,
                Status::Unregistered | Status::Dropped => s,
                _ => Status::Ready,
            };
            self.parts[p as usize].reverted |= s == Status::Landed;
        }
        self.cascade_drops();
        self.bump_main();
        Ok(Outcome::Dropped)
    }

    pub(crate) fn readmit(&mut self, part: PartId) -> Result<Outcome, Denied> {
        let p = self.parts.get_mut(part as usize).ok_or(Denied::Phase)?;
        if p.status != Status::Dropped {
            return Err(Denied::Phase);
        }
        p.status = Status::Ready;
        p.waited = 0;
        p.deferrals = 0;
        p.reverted = false;
        self.order.retain(|&q| q != part);
        self.order.push(part);
        Ok(Outcome::Applied)
    }
}
