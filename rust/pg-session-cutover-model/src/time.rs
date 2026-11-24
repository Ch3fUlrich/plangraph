use crate::*;

impl Model {
    pub(crate) fn bound_ticks(&self, class: UrgentClass) -> u8 {
        Params::ticks(self.p.budget(class).bound_secs)
    }

    fn sup_ticks(&self) -> u16 {
        u16::from(Params::ticks(self.p.supervisor_secs))
    }

    pub(crate) fn occur(&mut self, e: u8) -> Result<(), Denied> {
        let mode = self.mode;
        let ev = self.events.get_mut(usize::from(e)).ok_or(Denied::Index)?;
        if ev.stage != Stage::Future {
            return Err(Denied::Guard);
        }
        ev.stage = Stage::Occurred;
        // A role that is not switched keeps today's wake path, which carries it at once.
        ev.escalated = mode != Mode::Active;
        ev.binding = mode == Mode::Active;
        if mode != Mode::Active {
            self.wake();
        }
        Ok(())
    }

    pub(crate) fn wake(&mut self) {
        if self.session.live() {
            self.session.notify = true;
        }
    }

    /// A scan moves events into a digest (the scan cursor). It never acknowledges them: only
    /// a session's `Receive` or the alarm does.
    pub(crate) fn scan(&mut self, s: u8) -> Result<(), Denied> {
        let src = Self::source(s)?;
        if self.mode == Mode::Off {
            return Err(Denied::Mode);
        }
        self.since_scan[src] = 0;
        // A read that times out is visible at once; it never reads another source.
        self.blind[src] = self.hung[src];
        if !self.hung[src] {
            for e in self
                .events
                .iter_mut()
                .filter(|e| usize::from(e.source) == src)
            {
                if e.stage == Stage::Occurred {
                    e.stage = Stage::Detected;
                }
            }
        }
        Ok(())
    }

    /// A receipt hands the session every event of today's wake path and every scanned one.
    pub(crate) fn receive(&mut self) {
        let active = self.mode == Mode::Active;
        for e in &mut self.events {
            if e.open() && (e.escalated || (active && e.stage == Stage::Detected)) {
                (e.stage, e.seen) = (Stage::Received, true);
                e.session_at.get_or_insert(e.age);
            }
        }
    }

    pub(crate) fn fire_enabled(&self) -> bool {
        self.mode == Mode::Active
            && self.session.waiter == Waiter::Armed
            && self.events.iter().any(|e| e.stage == Stage::Detected)
    }

    /// The supervisor hands an unread event to today's wake path once it has reached its
    /// takeover age: the session is woken, or told in its turn.
    pub(crate) fn escalate(&mut self, e: u8) -> Result<(), Denied> {
        let ev = *self.events.get(usize::from(e)).ok_or(Denied::Index)?;
        let takeover = Params::ticks(self.p.budget(ev.class).takeover_secs);
        if !ev.binding || !ev.open() || ev.escalated || ev.age < takeover {
            return Err(Denied::Guard);
        }
        self.events[usize::from(e)].escalated = true;
        self.wake();
        Ok(())
    }

    /// The supervisor tells the operator about an event still unread at its alarm age. The
    /// operator is its first reader from then on, so the bound is kept whatever the session does;
    /// the event stays pending until a session receives it.
    pub(crate) fn alarm(&mut self, e: u8) -> Result<(), Denied> {
        let ev = *self.events.get(usize::from(e)).ok_or(Denied::Index)?;
        let alarm = Params::ticks(self.p.budget(ev.class).alarm_secs);
        let held = !ev.binding || !ev.open() || ev.alarmed;
        if self.has(Defect::NoAlarm) || held || ev.age < alarm {
            return Err(Denied::Guard);
        }
        let ev = &mut self.events[usize::from(e)];
        (ev.alarmed, ev.escalated) = (true, true);
        ev.operator_at.get_or_insert(ev.age);
        Ok(())
    }

    pub(crate) fn nudge_target(&self) -> bool {
        let s = &self.session;
        s.life == Life::Idle && (s.waiter != Waiter::Armed || self.stale())
    }

    /// Some binding event is still unread by the session after its takeover age: an armed wait
    /// does not help when the source hung or the fallback wake was lost.
    fn stale(&self) -> bool {
        self.events.iter().any(|e| {
            e.binding && e.open() && e.age >= Params::ticks(self.p.budget(e.class).takeover_secs)
        })
    }

    /// The session is working, or has a wait armed that can still fire. A queued wake is not
    /// enough: it can be late or lost, and then the supervisor must still act.
    fn attended(&self) -> bool {
        let s = &self.session;
        s.live() && (s.life == Life::Active || (s.waiter == Waiter::Armed && !self.stale()))
    }

    pub(crate) fn unattended(&self) -> bool {
        self.mode == Mode::Active && !self.attended()
    }

    /// Is some event past the age at which `step` must have happened? `step` picks the budget.
    fn overdue(&self, step: impl Fn(&Budget) -> u16, skip: impl Fn(&Evt) -> bool) -> bool {
        self.events.iter().any(|e| {
            let due = Params::ticks(step(&self.p.budget(e.class))) as u16 + self.sup_ticks();
            e.binding && e.open() && !skip(e) && u16::from(e.age) + 1 > due
        })
    }

    /// An obligation that must run before time may pass.
    fn due(&self) -> bool {
        // Delivery of a wake is not an obligation: it can be late or lost while time passes.
        let takeover = self.overdue(|b| b.takeover_secs, |e| e.escalated);
        let alarm = !self.has(Defect::NoAlarm) && self.overdue(|b| b.alarm_secs, |e| e.alarmed);
        let scan = u16::from(Params::ticks(self.p.scan_secs));
        let scans =
            self.mode != Mode::Off && self.since_scan.iter().any(|s| u16::from(*s) + 1 > scan);
        let asleep = !self.has(Defect::NoSupervisor)
            && self.unattended()
            && self.nudge_target()
            && u16::from(self.asleep_age) + 1 > self.sup_ticks();
        takeover || alarm || scans || asleep || self.session_overdue()
    }

    /// A live session has not read a binding event by its bound: the supervisor must end it,
    /// or time stops; time is not left to a session that ignores the event.
    fn session_overdue(&self) -> bool {
        self.session.live()
            && !self.has(Defect::NoKill)
            && self.events.iter().any(|e| {
                let bound = u16::from(self.bound_ticks(e.class));
                e.binding && e.open() && u16::from(e.age) + 1 > bound
            })
    }

    /// The supervisor ends a live session that has not read a binding event by its alarm age.
    /// What replaces it belongs to the next slice; meanwhile the alarm keeps the operator told.
    pub(crate) fn kill(&mut self) -> Result<(), Denied> {
        let late = self.events.iter().any(|e| {
            e.binding && e.open() && e.age >= Params::ticks(self.p.budget(e.class).alarm_secs)
        });
        if self.has(Defect::NoKill) || !self.session.live() || !late {
            return Err(Denied::Guard);
        }
        self.session = Session {
            life: Life::Stopped,
            waiter: Waiter::Dead,
            notify: false,
        };
        Ok(())
    }

    pub(crate) fn tick(&mut self) -> Result<(), Denied> {
        if self.due() {
            return Err(Denied::Deadline);
        }
        let cap = (MAX_TICKS + 1) as u8;
        for e in self.events.iter_mut().filter(|e| e.open()) {
            e.age = e.age.saturating_add(1).min(cap);
        }
        if self.mode != Mode::Off {
            self.since_scan = self.since_scan.map(|s| s.saturating_add(1).min(cap));
        }
        if self.unattended() {
            self.asleep_age = self.asleep_age.saturating_add(1).min(cap);
        }
        Ok(())
    }

    pub(crate) fn settle(&mut self) {
        if !self.unattended() {
            self.asleep_age = 0;
        }
        if self.mode == Mode::Off {
            self.since_scan = [0; MAX_SOURCES];
        }
    }
}
