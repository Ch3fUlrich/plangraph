use super::rules::place;
use super::select::ModelPool;
use super::*;

impl Model {
    fn train_mut(&mut self, train: TrainId) -> Result<&mut Train, Denied> {
        match self.trains.get_mut(train as usize) {
            Some(t) if !t.closed => Ok(t),
            _ => Err(Denied::Phase),
        }
    }

    /// The train lands: its parts are landed, main moves to it. A legacy train records no included list
    /// and verifies nothing; a landing never discards the candidate built on it.
    pub(crate) fn land(&mut self, train: TrainId, verified: bool) {
        let t = &mut self.trains[train as usize];
        t.closed = true;
        t.landed = true;
        if !verified {
            t.included = None;
        }
        let parts = t.parts.clone();
        self.release(&parts, Status::Landed);
        parts
            .iter()
            .for_each(|&p| self.parts[p as usize].verified = verified);
        self.main += 1;
    }

    fn settle(&mut self, train: TrainId, verified: bool) -> Result<Outcome, Denied> {
        let main = self.main;
        let t = self.train_mut(train)?;
        if matches!(t.kind, Kind::Speculative { .. }) {
            return Err(Denied::Phase);
        }
        if t.ci != Ci::Green || !t.bound {
            return Err(Denied::NotGreen);
        }
        if t.base != main {
            return Err(Denied::Stale);
        }
        if t.tested != Some(t.tree) {
            return Err(Denied::TreeChanged);
        }
        if !t.checked {
            return Err(Denied::Unverified);
        }
        self.land(train, verified);
        Ok(Outcome::Applied)
    }

    /// A CI result on a running train; a candidate that goes red is simply thrown away.
    fn ci_result(
        &mut self,
        train: TrainId,
        run: u8,
        green: bool,
        culprit: Option<PartId>,
    ) -> Result<Outcome, Denied> {
        let t = self.train_mut(train)?;
        // A result of an older run (a delayed callback) belongs to a tree that is no longer tested.
        if run != t.run {
            return Err(Denied::StaleRun);
        }
        let open_red = t.ci == Ci::Red && culprit.is_some();
        if t.ci != Ci::Running && !open_red {
            return Err(Denied::Phase);
        }
        if culprit.is_some_and(|c| !t.parts.contains(&c)) {
            return Err(Denied::Phase);
        }
        if matches!(t.kind, Kind::Speculative { .. }) && !green {
            self.discard_specs(None);
            return Ok(Outcome::Discarded);
        }
        if green {
            t.ci = Ci::Green;
            return Ok(Outcome::Applied);
        }
        Ok(self.red(train, culprit))
    }

    /// The tree of a train changed: a candidate built on it no longer is on its exact commit.
    fn retree(&mut self, train: TrainId) {
        self.trains[train as usize].tree += 1;
        self.trains[train as usize].checked = false;
        self.discard_specs(Some(train));
    }

    pub fn apply(&mut self, event: Event) -> Result<Outcome, Denied> {
        match event {
            Event::Register { part } => self.register(part),
            Event::Tick => {
                let cap = self.policy.bound.max(self.policy.cut_after);
                for p in self.parts.iter_mut().filter(|p| p.status == Status::Ready) {
                    p.waited = (p.waited + 1).min(cap);
                }
                Ok(Outcome::Applied)
            }
            Event::Select => self.select_ordinary(),
            Event::Join { part, train } => {
                let riding = self.trains.get(train as usize).map(|t| t.parts.clone());
                let riding = riding.ok_or(Denied::Phase)?;
                self.parent_ok(part, &riding)
                    .map_err(|_| Denied::Dependency)?;
                let ready = self
                    .parts
                    .get(part as usize)
                    .is_some_and(|p| p.status == Status::Ready);
                let (rule, ids) = (
                    self.rule(),
                    riding.iter().map(|&p| p as usize).collect::<Vec<_>>(),
                );
                let blocked = place(&ModelPool(self), &ids, rule, part as usize).is_some();
                let t = self.train_mut(train)?;
                if t.sealed {
                    return Err(Denied::CiStarted);
                }
                // The same rule as selection: a part deferred for overlap joins only once it is due.
                if t.kind != Kind::Ordinary || !ready || blocked {
                    return Err(Denied::Phase);
                }
                t.parts.push(part);
                t.included = Some(t.parts.clone());
                t.left_out.retain(|l| l.0 != part);
                if !t.ready_at_cut.contains(&part) {
                    t.ready_at_cut.push(part);
                }
                t.bound = false;
                t.checked = false;
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
                if !t.checked {
                    return Err(Denied::Unverified);
                }
                t.run += 1;
                t.sealed = true;
                t.frozen = t.parts.clone();
                t.ci = Ci::Running;
                t.tested = Some(t.tree);
                Ok(Outcome::Applied)
            }
            Event::CiResult { train, run, green } => self.ci_result(train, run, green, None),
            Event::CiRed {
                train,
                run,
                culprit,
            } => self.ci_result(train, run, false, Some(culprit)),
            Event::CiRedFix { train, run } => {
                let t = self.train_mut(train)?;
                if run != t.run {
                    return Err(Denied::StaleRun);
                }
                if t.ci != Ci::Running || matches!(t.kind, Kind::Speculative { .. }) {
                    return Err(Denied::Phase);
                }
                // After two red rounds the builder stops fixing in place: it drops or splits.
                if t.red_rounds + 1 >= 2 {
                    return Err(Denied::TwoRounds);
                }
                t.red_rounds += 1;
                t.ci = Ci::Red;
                self.discard_specs(Some(train));
                Ok(Outcome::Applied)
            }
            Event::FixInPlace { train } => {
                let t = self.train_mut(train)?;
                if t.ci != Ci::Red {
                    return Err(Denied::Phase);
                }
                t.ci = Ci::NotStarted;
                t.bound = false;
                self.retree(train);
                Ok(Outcome::Applied)
            }
            Event::Amend { train } => {
                let t = self.train_mut(train)?;
                if !t.sealed {
                    t.bound = false;
                }
                self.retree(train);
                Ok(Outcome::Applied)
            }
            Event::Settle { train } => self.settle(train, true),
            Event::SettleLegacy { train } => self.settle(train, false),
            Event::VerifyOutcome { train } => {
                let t = self.trains.get(train as usize).ok_or(Denied::Phase)?;
                if !t.landed || t.included.is_some() {
                    return Err(Denied::Phase);
                }
                let parts = t.parts.clone();
                if parts
                    .iter()
                    .any(|&p| self.parts[p as usize].status != Status::Landed)
                {
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
                // A candidate is never rebuilt: a changed predecessor discards it.
                if matches!(t.kind, Kind::Speculative { .. }) {
                    return Err(Denied::Phase);
                }
                t.base = main;
                t.bound = false;
                if t.sealed {
                    t.ci = Ci::NotStarted;
                }
                self.retree(train);
                Ok(Outcome::Applied)
            }
            Event::StartRetry => self.start_retry(),
            Event::Conflict { train, part } => self.conflict(train, part),
            Event::Adopt { train, part } => self.adopt(train, part),
            Event::Drop { part } => self.drop_part(part),
            Event::Revert { part } => self.revert(part),
            Event::Readmit { part } => self.readmit(part),
            Event::MainMove => {
                self.bump_main();
                Ok(Outcome::Applied)
            }
            Event::Restart => {
                // The journal keeps ages, deferrals, red rounds and CI state; the in-memory binding of
                // an open train is gone.
                for t in self.trains.iter_mut().filter(|t| !t.closed && !t.sealed) {
                    t.bound = false;
                }
                Ok(Outcome::Applied)
            }
            Event::Speculate { pred } => self.speculate(pred),
            Event::Promote { train } => self.promote(train),
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
                t.checked = false;
                Ok(Outcome::Applied)
            }
            Event::OmitLeftOut { train, part } => {
                let t = self.train_mut(train)?;
                if !t.left_out.iter().any(|l| l.0 == part) {
                    return Err(Denied::Phase);
                }
                t.left_out.retain(|l| l.0 != part);
                t.checked = false;
                Ok(Outcome::Applied)
            }
            Event::VerifyLeftOut { train, part } => self.verify_left_out(train, part),
            Event::VerifyComplete { train } => self.verify_complete(train),
        }
    }
}
