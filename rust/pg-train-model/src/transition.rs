use super::*;

impl Model {
    fn train_mut(&mut self, train: TrainId) -> Result<&mut Train, Denied> {
        match self.trains.get_mut(train as usize) {
            Some(t) if !t.closed => Ok(t),
            _ => Err(Denied::Phase),
        }
    }

    /// A part waiting in a retry group whose parent dropped cannot land: it goes back to ready.
    pub(crate) fn cascade_drops(&mut self) {
        loop {
            let orphan = self
                .groups
                .iter()
                .flat_map(|g| g.parts.iter().copied())
                .find(|&p| {
                    self.parts[p as usize]
                        .spec
                        .parent
                        .is_some_and(|q| self.parts[q as usize].status == Status::Dropped)
                });
            let Some(p) = orphan else { break };
            for g in &mut self.groups {
                g.parts.retain(|&q| q != p);
            }
            self.groups.retain(|g| !g.parts.is_empty());
            self.parts[p as usize].status = Status::Ready;
        }
    }

    pub(crate) fn release(&mut self, parts: &[PartId], status: Status) {
        parts
            .iter()
            .for_each(|&p| self.parts[p as usize].status = status);
    }

    /// `part` and every part above it in `set`.
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

    pub fn apply(&mut self, event: Event) -> Result<Outcome, Denied> {
        match event {
            Event::Register { part } => {
                let p = self.parts.get(part as usize).ok_or(Denied::Phase)?;
                let parent_known = p.spec.parent.is_none_or(|q| self.order.contains(&q));
                if p.status != Status::Unregistered || !parent_known {
                    return Err(Denied::Phase);
                }
                self.parts[part as usize].status = Status::Ready;
                self.order.push(part);
                Ok(Outcome::Applied)
            }
            Event::Tick => {
                let cap = self.policy.bound.max(self.policy.cut_after);
                for p in self.parts.iter_mut().filter(|p| p.status == Status::Ready) {
                    p.waited = (p.waited + 1).min(cap);
                }
                Ok(Outcome::Applied)
            }
            Event::Select => self.select_ordinary(),
            Event::Join { part, train } => {
                let target = self.policy.target as usize;
                let riding = self.trains.get(train as usize).map(|t| t.parts.clone());
                let riding = riding.ok_or(Denied::Phase)?;
                self.parent_ok(part, &riding)
                    .map_err(|_| Denied::Dependency)?;
                let ready = self
                    .parts
                    .get(part as usize)
                    .is_some_and(|p| p.status == Status::Ready);
                let t = self.train_mut(train)?;
                if t.sealed {
                    return Err(Denied::CiStarted);
                }
                if t.kind != Kind::Ordinary || !ready || t.parts.len() >= target {
                    return Err(Denied::Phase);
                }
                t.parts.push(part);
                t.included = Some(t.parts.clone());
                t.bound = false;
                self.parts[part as usize].status = Status::InTrain(train);
                Ok(Outcome::Applied)
            }
            Event::Bind {
                train,
                build,
                base,
                policy,
            } => {
                let main = self.main;
                let t = self.train_mut(train)?;
                if build != train || base != t.base || policy != POLICY {
                    return Err(Denied::BindMismatch);
                }
                if t.base != main {
                    return Err(Denied::Stale);
                }
                t.bound = true;
                Ok(Outcome::Applied)
            }
            Event::StartCi { train } => {
                let t = self.train_mut(train)?;
                if !t.bound {
                    return Err(Denied::Unbound);
                }
                if t.ci != Ci::NotStarted {
                    return Err(Denied::Phase);
                }
                t.sealed = true;
                t.frozen = t.parts.clone();
                t.ci = Ci::Running;
                Ok(Outcome::Applied)
            }
            Event::CiResult { train, green } => {
                if self.train_mut(train)?.ci != Ci::Running {
                    return Err(Denied::Phase);
                }
                if green {
                    self.trains[train as usize].ci = Ci::Green;
                    Ok(Outcome::Applied)
                } else {
                    Ok(self.red(train, None))
                }
            }
            Event::CiRed { train, culprit } => {
                let t = self.train_mut(train)?;
                if t.ci != Ci::Running || !t.parts.contains(&culprit) {
                    return Err(Denied::Phase);
                }
                Ok(self.red(train, Some(culprit)))
            }
            Event::Settle { train } | Event::SettleLegacy { train } => {
                let legacy = matches!(event, Event::SettleLegacy { .. });
                let main = self.main;
                let t = self.train_mut(train)?;
                if t.ci != Ci::Green || !t.bound {
                    return Err(Denied::NotGreen);
                }
                if t.base != main {
                    return Err(Denied::Stale);
                }
                t.closed = true;
                t.landed = true;
                if legacy {
                    t.included = None;
                }
                let parts = t.parts.clone();
                self.release(&parts, Status::Landed);
                parts
                    .iter()
                    .for_each(|&p| self.parts[p as usize].verified = !legacy);
                self.main += 1;
                Ok(Outcome::Applied)
            }
            Event::VerifyOutcome { train } => {
                let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
                if !t.landed || t.included.is_some() {
                    return Err(Denied::Phase);
                }
                let parts = t.parts.clone();
                let alive = parts
                    .iter()
                    .all(|&p| self.parts[p as usize].status == Status::Landed);
                if !alive {
                    return Err(Denied::Phase);
                }
                parts
                    .iter()
                    .for_each(|&p| self.parts[p as usize].verified = true);
                Ok(Outcome::Applied)
            }
            Event::Rebuild { train } => {
                let main = self.main;
                let t = self.train_mut(train)?;
                t.base = main;
                t.bound = false;
                if t.sealed {
                    t.ci = Ci::NotStarted;
                }
                Ok(Outcome::Applied)
            }
            Event::StartRetry => self.start_retry(),
            Event::Conflict { train, part } => {
                let t = self.train_mut(train)?;
                if t.sealed || !t.parts.contains(&part) {
                    return Err(Denied::Phase);
                }
                let parts = t.parts.clone();
                let out = self.with_descendants(part, &parts);
                let t = &mut self.trains[train as usize];
                t.parts.retain(|p| !out.contains(p));
                t.included = Some(t.parts.clone());
                t.bound = false;
                t.closed = t.parts.is_empty();
                self.release(&out, Status::Ready);
                Ok(Outcome::Applied)
            }
            Event::Drop { part } => {
                let p = self.parts.get(part as usize).ok_or(Denied::Phase)?;
                // The part leaves; what rode or waited on it goes back to ready, to wait for a landed parent.
                let leaving: Vec<PartId> = match p.status {
                    Status::Ready => vec![part],
                    Status::Held => {
                        let g = self.groups.iter().find(|g| g.parts.contains(&part));
                        self.with_descendants(part, g.map_or(&[], |g| &g.parts))
                    }
                    Status::InTrain(t) if !self.trains[t as usize].sealed => {
                        self.with_descendants(part, &self.trains[t as usize].parts)
                    }
                    _ => return Err(Denied::Phase),
                };
                for g in &mut self.groups {
                    g.parts.retain(|q| !leaving.contains(q));
                }
                self.groups.retain(|g| !g.parts.is_empty());
                if let Status::InTrain(t) = self.parts[part as usize].status {
                    let t = &mut self.trains[t as usize];
                    t.parts.retain(|q| !leaving.contains(q));
                    t.included = Some(t.parts.clone());
                    t.bound = false;
                    t.closed = t.parts.is_empty();
                }
                self.release(&leaving, Status::Ready);
                self.parts[part as usize].status = Status::Dropped;
                self.cascade_drops();
                Ok(Outcome::Dropped)
            }
            Event::Revert { part } => {
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
                        let t = &mut self.trains[t as usize];
                        t.parts.retain(|&q| q != p);
                        t.included = Some(t.parts.clone());
                        t.bound = false;
                        t.closed = t.parts.is_empty();
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
                }
                self.cascade_drops();
                self.main += 1;
                Ok(Outcome::Dropped)
            }
            Event::Readmit { part } => {
                let p = self.parts.get_mut(part as usize).ok_or(Denied::Phase)?;
                if p.status != Status::Dropped {
                    return Err(Denied::Phase);
                }
                p.status = Status::Ready;
                p.waited = 0;
                self.order.retain(|&q| q != part);
                self.order.push(part);
                Ok(Outcome::Applied)
            }
            Event::MainMove => {
                self.main += 1;
                Ok(Outcome::Applied)
            }
            Event::RecordLeftOut {
                train,
                part,
                disposition,
            } => {
                let ready = self
                    .parts
                    .get(part as usize)
                    .is_some_and(|p| p.status == Status::Ready);
                let t = self.train_mut(train)?;
                if t.sealed || !ready {
                    return Err(Denied::Phase);
                }
                t.left_out.retain(|l| l.0 != part);
                t.left_out.push((part, disposition));
                Ok(Outcome::Applied)
            }
            Event::VerifyLeftOut { train, part } => self.verify_left_out(train, part),
        }
    }

    /// The relations that must hold after every event.
    pub fn invariants_hold(&self) -> bool {
        self.membership_holds()
            && self.trains_hold()
            && self.dependencies_hold()
            && self.retry_bounded()
    }

    /// A part is in at most one train, and the train it names holds it.
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

    fn trains_hold(&self) -> bool {
        let cap = self.policy.target as usize;
        let ordinary_live = self
            .trains
            .iter()
            .filter(|t| t.kind == Kind::Ordinary && !t.closed)
            .count();
        let retry_live = self
            .trains
            .iter()
            .filter(|t| matches!(t.kind, Kind::Retry { .. }) && !t.closed)
            .count();
        ordinary_live <= 1
            && retry_live <= 1
            // Retry precedence: no ordinary train is open while retry work is pending or running.
            && (ordinary_live == 0 || (retry_live == 0 && self.groups.is_empty()))
            && self.trains.iter().all(|t| {
                // No part joined a train after its CI started.
                (!t.sealed || t.parts == t.frozen || t.closed && t.landed && t.parts == t.frozen)
                    && (t.kind != Kind::Ordinary || t.parts.len() <= cap)
                    && (!t.landed || t.ci == Ci::Green && t.bound)
                    && (t.ci != Ci::Running || t.sealed && t.bound)
            })
            && self.trains.iter().filter(|t| t.landed).count() <= self.main as usize
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
                    self.parts[p as usize]
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
