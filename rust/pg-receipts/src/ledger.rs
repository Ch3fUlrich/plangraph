use std::collections::BTreeMap;

use super::*;

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub(crate) struct Row {
    pub spec: Spec,
    pub parent: Option<Id>,
    pub manifest: Vec<(Id, Spec)>,
    pub state: State,
    pub generation: u32,
    pub payload: Option<Id>,
    pub deadline: u32,
    pub remaining: u32,
    pub delivery: Vec<(Ticket, Delivery)>,
    pub resend_ready: bool,
    pub disposition: Option<Id>,
    pub cancel: Option<Id>,
    pub cancels: Option<Id>,
    pub process_stopped: bool,
    pub replaced_by: Option<Id>,
}

impl Row {
    pub fn new(spec: Spec, generation: u32, parent: Option<Id>) -> Self {
        Self {
            spec,
            parent,
            manifest: Vec::new(),
            state: if parent.is_some() {
                State::Planned
            } else {
                State::Pending
            },
            generation,
            payload: None,
            deadline: 0,
            remaining: spec.resends,
            delivery: Vec::new(),
            resend_ready: false,
            disposition: None,
            cancel: None,
            cancels: None,
            process_stopped: false,
            replaced_by: None,
        }
    }

    pub fn safe(&self) -> bool {
        self.delivery
            .iter()
            .all(|(_, d)| matches!(d, Delivery::Fenced | Delivery::Applied))
    }

    pub fn effects(&self) -> usize {
        self.delivery
            .iter()
            .filter(|(_, d)| *d == Delivery::Applied)
            .count()
    }

    pub fn gated(&self) -> bool {
        matches!(
            self.spec.class,
            Class::Gated | Class::Launch | Class::Protected { .. }
        )
    }

    pub fn settled(&self) -> bool {
        matches!(self.state, State::Ok | State::Failed) && self.safe()
    }
}

/// The durable keeper rows and destination outcomes in a deterministic model clock.
#[derive(Clone, Debug, Default, Eq, PartialEq, Hash)]
pub struct Ledger {
    pub(crate) now: u32,
    pub(crate) generation: u32,
    pub(crate) rows: BTreeMap<Id, Row>,
}

impl Ledger {
    pub fn tick(&mut self) {
        self.now = self.now.saturating_add(1);
    }

    pub fn state(&self, id: Id) -> Option<State> {
        self.rows.get(&id).map(|row| row.state)
    }

    pub fn effects(&self, id: Id) -> usize {
        self.rows.get(&id).map_or(0, Row::effects)
    }

    pub fn stamp(&self, id: Id) -> Option<Stamp> {
        self.rows
            .get(&id)
            .map(|row| Stamp(row.generation, row.delivery.len() as u32))
    }

    pub fn read(
        &self,
        id: Id,
        credential: Id,
        scoped_successor: Option<(Id, u32)>,
    ) -> Option<State> {
        let row = self.rows.get(&id)?;
        (row.spec.credential == credential || scoped_successor == Some((id, self.generation)))
            .then_some(row.state)
    }

    pub fn begin(&mut self, id: Id, spec: Spec, children: &[(Id, Spec)]) -> Result<State, Refused> {
        if let Some(row) = self.rows.get(&id) {
            return if row.spec == spec && row.manifest == children {
                Ok(row.state)
            } else {
                Err(Refused::Conflict)
            };
        }
        let mut additions = BTreeMap::new();
        additions.insert(id, Row::new(spec, self.generation, None));
        for &(child, spec) in children {
            if additions
                .insert(child, Row::new(spec, self.generation, Some(id)))
                .is_some()
            {
                return Err(Refused::Conflict);
            }
        }
        for (&candidate_id, candidate) in &additions {
            if self.rows.contains_key(&candidate_id)
                || self
                    .rows
                    .values()
                    .chain(
                        additions
                            .iter()
                            .filter_map(|(&other, row)| (other != candidate_id).then_some(row)),
                    )
                    .any(|row| {
                        (candidate.spec.conflict.is_some()
                            && candidate.spec.conflict == row.spec.conflict
                            && !row.settled()
                            && !self.inactive_child(row))
                            || (candidate.spec.binding.is_some()
                                && candidate.spec.binding == row.spec.binding
                                && row.state != State::Failed
                                && !self.inactive_child(row))
                    })
            {
                return Err(Refused::Conflict);
            }
        }
        additions.get_mut(&id).unwrap().manifest = children.to_vec();
        self.rows.append(&mut additions);
        Ok(State::Pending)
    }

    pub(crate) fn inactive_child(&self, row: &Row) -> bool {
        row.state == State::Planned
            && row
                .parent
                .is_some_and(|parent| self.state(parent) == Some(State::Failed))
    }

    pub fn obligations(&self) -> Vec<Id> {
        self.rows
            .iter()
            .filter_map(|(&id, row)| {
                let due = row
                    .parent
                    .is_some_and(|parent| self.state(parent) == Some(State::Ok));
                (row.disposition.is_none()
                    && match row.state {
                        State::Pending | State::Unknown => true,
                        State::Dispatching | State::Admitted => self.now >= row.deadline,
                        State::Planned => due,
                        State::Failed => due && row.replaced_by.is_none(),
                        State::Ok => false,
                    })
                .then_some(id)
            })
            .collect()
    }

    pub fn takeover(&mut self, generation: u32, predecessor_fenced: bool) -> Result<(), Refused> {
        if !predecessor_fenced
            || self.generation != generation
            || self.rows.values().any(|row| {
                (row.state == State::Dispatching && row.gated())
                    || (matches!(row.spec.class, Class::Protected { .. }) && !row.safe())
            })
        {
            return Err(Refused::Guard);
        }
        self.generation = generation.checked_add(1).ok_or(Refused::Guard)?;
        for row in self.rows.values_mut().filter(|row| {
            !row.settled()
                || row.parent.is_some()
                || (row.spec.class == Class::Launch && !row.process_stopped)
        }) {
            row.generation = self.generation;
        }
        Ok(())
    }

    pub fn admit(&mut self, ticket: Ticket) -> Result<(), Refused> {
        let row = self.rows.get_mut(&ticket.id).ok_or(Refused::Missing)?;
        if !row.gated()
            || row.state != State::Dispatching
            || row.cancel.is_some()
            || self.now >= ticket.deadline
            || ticket.stamp.0 != self.generation
            || row.delivery.last() != Some(&(ticket, Delivery::InFlight))
        {
            return Err(Refused::Guard);
        }
        row.delivery.last_mut().unwrap().1 = Delivery::Reserved;
        row.state = State::Admitted;
        Ok(())
    }

    pub fn effect(&mut self, ticket: Ticket) -> Result<(), Refused> {
        let row = self.rows.get_mut(&ticket.id).ok_or(Refused::Missing)?;
        if row.cancel.is_some() {
            return Err(Refused::Guard);
        }
        Self::advance_delivery(row, ticket, Delivery::Reserved, Delivery::Applied)
    }

    pub fn deliver(&mut self, ticket: Ticket) -> Result<(), Refused> {
        let row = self.rows.get_mut(&ticket.id).ok_or(Refused::Missing)?;
        if row.gated() {
            return Err(Refused::Guard);
        }
        Self::advance_delivery(row, ticket, Delivery::InFlight, Delivery::Applied)
    }

    /// Confirm that the process created by the applied launch has stopped.
    pub fn confirm_stopped(&mut self, ticket: Ticket) -> Result<(), Refused> {
        let row = self.rows.get_mut(&ticket.id).ok_or(Refused::Missing)?;
        if row.cancel.is_none() || !row.delivery.contains(&(ticket, Delivery::Applied)) {
            return Err(Refused::Guard);
        }
        row.process_stopped = true;
        Ok(())
    }

    /// A destination barrier rules out an attempt that has not yet applied.
    pub fn fence(&mut self, ticket: Ticket) -> Result<(), Refused> {
        let row = self.rows.get_mut(&ticket.id).ok_or(Refused::Missing)?;
        let (_, delivery) = row
            .delivery
            .iter_mut()
            .find(|(known, _)| *known == ticket)
            .ok_or(Refused::Missing)?;
        match delivery {
            Delivery::InFlight | Delivery::Reserved => *delivery = Delivery::Fenced,
            _ => return Err(Refused::Guard),
        }
        Ok(())
    }

    fn advance_delivery(
        row: &mut Row,
        ticket: Ticket,
        from: Delivery,
        to: Delivery,
    ) -> Result<(), Refused> {
        let (_, delivery) = row
            .delivery
            .iter_mut()
            .find(|(known, _)| *known == ticket)
            .ok_or(Refused::Missing)?;
        if *delivery != from {
            return Err(Refused::Guard);
        }
        *delivery = to;
        Ok(())
    }
}
