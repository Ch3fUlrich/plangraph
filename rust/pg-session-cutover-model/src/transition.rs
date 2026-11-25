use crate::*;

impl Model {
    /// Apply one atomic transition. A refused event leaves the model exactly as it was.
    pub fn apply(&mut self, event: Event) -> Result<(), Denied> {
        let mut next = self.clone();
        next.step(event)?;
        next.settle();
        *self = next;
        Ok(())
    }

    fn step(&mut self, event: Event) -> Result<(), Denied> {
        let life = self.session.life;
        match event {
            Event::Tick => self.tick()?,
            Event::Occur(e) => self.occur(e)?,
            Event::WakeLost => {
                self.guard(self.session.notify)?;
                self.session.notify = false;
            }
            Event::BeginTurn => {
                self.guard(life == Life::Idle && self.session.notify)?;
                (self.session.life, self.session.notify) = (Life::Active, false);
            }
            Event::EndTurn => {
                self.guard(life == Life::Active)?;
                self.session.life = Life::Idle;
            }
            Event::Receive => {
                self.guard(life == Life::Active)?;
                self.receive();
            }
            Event::Crash => {
                self.guard(life != Life::Stopped)?;
                self.session = Session {
                    life: Life::Stopped,
                    notify: false,
                };
            }
            Event::Escalate(e) => self.escalate(e)?,
            Event::Alarm(e) => self.alarm(e)?,
            Event::Nudge => {
                self.guard(self.nudge_allowed())?;
                // Each nudge gives the wake one more supervisor pass.
                (self.session.notify, self.asleep_age) = (true, 0);
            }
            Event::Recover => self.recover()?,
            Event::GrantGo => self.go = true,
            Event::Activate => self.activate()?,
            Event::Rollback => {
                self.guard(self.mode == Mode::Active)?;
                self.force_off();
            }
            Event::RevokeGo => {
                self.go = false;
                if self.mode == Mode::Active {
                    self.force_off();
                }
            }
        }
        Ok(())
    }

    fn guard(&self, ok: bool) -> Result<(), Denied> {
        if ok {
            Ok(())
        } else {
            Err(Denied::Guard)
        }
    }

    /// Switching on needs a live GO; what is pending is held to its deadline from then on, in
    /// every mode.
    fn activate(&mut self) -> Result<(), Denied> {
        self.guard(self.mode == Mode::Off && self.go)?;
        self.events
            .iter_mut()
            .filter(|e| e.open())
            .for_each(|e| e.binding = true);
        self.mode = Mode::Active;
        Ok(())
    }

    /// Back to today's behaviour: every unread event goes to the existing wake path; its deadline
    /// stays.
    fn force_off(&mut self) {
        self.mode = Mode::Off;
        let mut any = false;
        for e in self.events.iter_mut().filter(|e| e.open()) {
            (e.escalated, any) = (true, true);
        }
        if any {
            self.wake();
        }
    }
}
