use crate::*;

type Step = fn(&Budget) -> u16;

impl Model {
    pub(crate) fn at(&self, e: &Evt, step: Step) -> u8 {
        Params::ticks(step(&self.p.budget(e.class)))
    }

    pub(crate) fn pending(&self) -> impl Iterator<Item = &Evt> {
        self.events.iter().filter(|e| e.binding && e.open())
    }

    fn sup(&self) -> u16 {
        u16::from(Params::ticks(self.p.supervisor_secs))
    }

    pub(crate) fn occur(&mut self, e: u8) -> Result<(), Denied> {
        let active = self.mode == Mode::Active;
        let ev = self.events.get_mut(usize::from(e)).ok_or(Denied::Guard)?;
        if ev.stage != Stage::Future {
            return Err(Denied::Guard);
        }
        // A role that is not switched keeps today's wake path, which carries it at once.
        (ev.stage, ev.binding, ev.escalated) = (Stage::Occurred, active, !active);
        if !active {
            self.wake();
        }
        Ok(())
    }

    pub(crate) fn wake(&mut self) {
        self.session.notify |= self.session.life != Life::Stopped;
    }

    pub(crate) fn receive(&mut self) {
        for e in self.events.iter_mut().filter(|e| e.open() && e.escalated) {
            (e.stage, e.session_at) = (Stage::Received, e.session_at.or(Some(e.age)));
        }
    }

    pub(crate) fn escalate(&mut self, e: u8) -> Result<(), Denied> {
        let ev = *self.events.get(usize::from(e)).ok_or(Denied::Guard)?;
        let due = ev.binding && ev.open() && !ev.escalated;
        if !due || ev.age < self.at(&ev, |b| b.takeover_secs) {
            return Err(Denied::Guard);
        }
        self.events[usize::from(e)].escalated = true;
        self.wake();
        Ok(())
    }

    /// The operator is told at the alarm age; the event stays pending for the session.
    pub(crate) fn alarm(&mut self, e: u8) -> Result<(), Denied> {
        let ev = *self.events.get(usize::from(e)).ok_or(Denied::Guard)?;
        let due = ev.binding && ev.open() && ev.operator_at.is_none();
        if self.has(Defect::NoAlarm) || !due || ev.age < self.at(&ev, |b| b.alarm_secs) {
            return Err(Denied::Guard);
        }
        let ev = &mut self.events[usize::from(e)];
        (ev.escalated, ev.operator_at) = (true, Some(ev.age));
        Ok(())
    }

    /// The supervisor owes a retry: the role is active, or an event is still unread after a
    /// switch-off. `Nudge` and its deadline both use this one eligibility.
    fn owed(&self) -> bool {
        self.mode == Mode::Active || self.pending().next().is_some()
    }

    pub(crate) fn nudge_allowed(&self) -> bool {
        self.owed() && !self.has(Defect::NoSupervisor) && self.session.life == Life::Idle
    }

    pub(crate) fn unattended(&self) -> bool {
        self.owed() && self.session.life == Life::Idle
    }

    fn overdue(&self, step: Step, skip: fn(&Evt) -> bool) -> bool {
        let late = |e: &Evt| u16::from(e.age) + 1 > u16::from(self.at(e, step)) + self.sup();
        self.pending().any(|e| !skip(e) && late(e))
    }

    fn due(&self) -> bool {
        let takeover = self.overdue(|b| b.takeover_secs, |e| e.escalated);
        let alarm = !self.has(Defect::NoAlarm)
            && self.overdue(|b| b.alarm_secs, |e| e.operator_at.is_some());
        let asleep = !self.has(Defect::NoSupervisor)
            && self.unattended()
            && u16::from(self.asleep_age) + 1 > self.sup();
        // Time does not pass a session's bound: it reads (`Receive`) or the authorized recovery
        // replaces an idle or dead session, whatever the session was doing.
        let bound = |e: &Evt| u16::from(e.age) + 1 > u16::from(self.at(e, |b| b.bound_secs));
        let session = !self.has(Defect::NoRecovery) && self.pending().any(bound);
        takeover || alarm || asleep || session
    }

    /// The authorized recovery takes an idle or a dead session, never a working one. Its
    /// successor reads what the wake path handed over; any other event stays pending.
    pub(crate) fn recover(&mut self) -> Result<(), Denied> {
        let late = self
            .pending()
            .any(|e| e.age >= self.at(e, |b| b.alarm_secs));
        let allowed = self.session.life != Life::Active && !self.has(Defect::NoRecovery);
        if !allowed || !late {
            return Err(Denied::Guard);
        }
        self.session = Session {
            life: Life::Idle,
            notify: false,
        };
        self.asleep_age = 0;
        self.receive();
        Ok(())
    }

    pub(crate) fn tick(&mut self) -> Result<(), Denied> {
        if self.due() {
            return Err(Denied::Deadline);
        }
        self.events
            .iter_mut()
            .filter(|e| e.open())
            .for_each(|e| e.age = (e.age + 1).min(MAX_TICKS as u8));
        if self.unattended() {
            self.asleep_age += 1;
        }
        Ok(())
    }

    pub(crate) fn settle(&mut self) {
        if !self.unattended() {
            self.asleep_age = 0;
        }
    }
}
