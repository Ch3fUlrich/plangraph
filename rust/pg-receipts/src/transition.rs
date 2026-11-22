use crate::{ledger::Row, *};

impl Ledger {
    /// Apply one guarded verb. A refusal leaves the ledger unchanged.
    pub fn apply(&mut self, id: Id, stamp: Stamp, step: Step) -> Result<Reply, Refused> {
        let row = self.rows.get(&id).ok_or(Refused::Missing)?;
        if self.stamp(id) != Some(stamp) || stamp.0 != self.generation {
            return Err(Refused::Stale);
        }
        let due = row
            .parent
            .is_some_and(|parent| self.state(parent) == Some(State::Ok));
        let mut next = row.clone();
        let mut insert = None;
        match step {
            Step::Bind(payload)
                if matches!(row.state, State::Planned | State::Pending)
                    && row.payload.is_none_or(|bound| bound == payload) =>
            {
                next.payload = Some(payload);
            }
            Step::Activate if row.state == State::Planned && due && row.disposition.is_none() => {
                next.state = State::Pending;
            }
            Step::Dispatch if row.can_dispatch() => {
                let deadline = self.now.checked_add(1).ok_or(Refused::Guard)?;
                let attempt = stamp.1.checked_add(1).ok_or(Refused::Guard)?;
                let ticket = Ticket {
                    id,
                    stamp: Stamp(stamp.0, attempt),
                    deadline,
                };
                if row.state == State::Unknown {
                    next.remaining -= 1;
                }
                next.state = State::Dispatching;
                next.resend_ready = false;
                next.deadline = deadline;
                next.delivery.push((ticket, Delivery::InFlight));
                self.rows.insert(id, next);
                return Ok(Reply::Send(ticket));
            }
            Step::Expire if row.state == State::Dispatching && self.now >= row.deadline => {
                if row.gated() {
                    next.delivery.last_mut().unwrap().1 = Delivery::Fenced;
                    next.state = State::Pending;
                } else {
                    next.state = State::Unknown;
                }
            }
            Step::Finish(success) if matches!(row.state, State::Dispatching | State::Admitted) => {
                next.settle(success)?;
            }
            Step::Observe(proof)
                if matches!(
                    row.state,
                    State::Dispatching | State::Admitted | State::Unknown
                ) || (row.state == State::Pending
                    && (row.cancel.is_some()
                        || row.disposition.is_some()
                        || !row.delivery.is_empty())
                    && proof == Proof::FencedAbsent) =>
            {
                match proof {
                    Proof::Present => next.settle(true)?,
                    Proof::FencedAbsent => next.settle(false)?,
                    Proof::Undecidable if row.state != State::Dispatching || !row.gated() => {
                        next.state = State::Unknown;
                    }
                    Proof::AbsentNow
                        if matches!(row.spec.class, Class::Budgeted) && row.effects() == 0 =>
                    {
                        next.state = State::Unknown;
                        next.resend_ready = true;
                    }
                    _ => return Err(Refused::Guard),
                }
            }
            Step::Resolve(decision)
                if decision != 0
                    && row.disposition.is_none()
                    && (row.state == State::Unknown
                        || (row.state == State::Failed && due)
                        || (row.state == State::Pending
                            && matches!(
                                row.spec.class,
                                Class::Protected {
                                    authority_at_destination: false
                                }
                            ))) =>
            {
                next.disposition = Some(decision);
                next.resend_ready = false;
            }
            Step::RetryChild(invocation)
                if row.state == State::Failed
                    && due
                    && row.spec.retryable
                    && row.replaced_by.is_none()
                    && row.disposition.is_none() =>
            {
                if self.rows.contains_key(&invocation)
                    || self.rows.values().any(|other| {
                        (row.spec.conflict.is_some()
                            && row.spec.conflict == other.spec.conflict
                            && !other.settled()
                            && !self.inactive_child(other))
                            || (row.spec.binding.is_some()
                                && row.spec.binding == other.spec.binding
                                && other.state != State::Failed
                                && !self.inactive_child(other))
                    })
                {
                    return Err(Refused::Conflict);
                }
                let mut retry = Row::new(row.spec, self.generation, row.parent);
                retry.state = State::Pending;
                retry.payload = row.payload;
                next.replaced_by = Some(invocation);
                insert = Some((invocation, retry));
            }
            Step::RequestCancel(invocation) if row.cancel == Some(invocation) => {}
            Step::RequestCancel(invocation)
                if row.spec.class == Class::Launch && row.cancel.is_none() =>
            {
                if self.rows.contains_key(&invocation) {
                    return Err(Refused::Conflict);
                }
                let mut cancel = Row::new(
                    Spec {
                        class: Class::Gated,
                        conflict: None,
                        ..row.spec
                    },
                    self.generation,
                    None,
                );
                cancel.spec.binding = None;
                cancel.cancels = Some(id);
                cancel.payload = Some(id);
                next.cancel = Some(invocation);
                insert = Some((invocation, cancel));
            }
            _ => return Err(Refused::Guard),
        }
        if next.state == State::Ok
            && next.cancels.is_some_and(|launch| {
                let launch = &self.rows[&launch];
                launch.state != State::Failed && !launch.process_stopped
            })
        {
            return Err(Refused::Guard);
        }
        let result = next.state;
        self.rows.insert(id, next);
        if let Some((id, row)) = insert {
            self.rows.insert(id, row);
        }
        Ok(Reply::State(result))
    }
}

impl Row {
    fn can_dispatch(&self) -> bool {
        !matches!(
            self.spec.class,
            Class::Protected {
                authority_at_destination: false
            }
        ) && self.cancel.is_none()
            && self.payload.is_some()
            && self.disposition.is_none()
            && (self.state == State::Pending
                || (self.state == State::Unknown && self.resend_ready && self.remaining > 0))
    }

    fn settle(&mut self, success: bool) -> Result<(), Refused> {
        if !self.safe() || (self.effects() > 0) != success {
            return Err(Refused::Guard);
        }
        self.state = if success { State::Ok } else { State::Failed };
        Ok(())
    }
}
