use crate::*;

impl Model {
    /// Apply one atomic transition. A refused event leaves the model exactly as it was.
    pub fn apply(&mut self, event: Event) -> Result<Outcome, Denied> {
        let mut next = self.clone();
        next.step(event)?;
        next.settle();
        *self = next;
        Ok(Outcome::Applied)
    }

    fn step(&mut self, event: Event) -> Result<(), Denied> {
        match event {
            Event::Tick => self.tick()?,
            Event::Occur(e) => self.occur(e)?,
            Event::Scan(s) => self.scan(s)?,
            Event::Hang(s) => self.hung[Self::source(s)?] = true,
            Event::Heal(s) => self.hung[Self::source(s)?] = false,
            Event::WaiterFire => {
                if !self.fire_enabled() {
                    return Err(Denied::Guard);
                }
                self.session.waiter = Waiter::Fired;
                self.session.notify = true;
            }
            Event::WaiterDie => {
                if self.session.waiter != Waiter::Armed {
                    return Err(Denied::Guard);
                }
                self.session.waiter = Waiter::Dead;
            }
            Event::WakeLost => {
                if !self.session.notify {
                    return Err(Denied::Guard);
                }
                self.session.notify = false;
            }
            Event::BeginTurn => {
                let s = &mut self.session;
                if s.life != Life::Idle || !s.notify {
                    return Err(Denied::Guard);
                }
                (s.life, s.notify) = (Life::Active, false);
            }
            Event::EndTurn => {
                self.working()?;
                self.session.life = Life::Idle;
            }
            Event::Arm => {
                self.working()?;
                if self.mode != Mode::Active {
                    return Err(Denied::Mode);
                }
                if self.session.waiter == Waiter::Armed {
                    return Err(Denied::Guard);
                }
                self.session.waiter = Waiter::Armed;
            }
            Event::Receive => {
                self.working()?;
                self.receive();
            }
            Event::Crash => {
                if !self.session.live() {
                    return Err(Denied::Guard);
                }
                self.session = Session {
                    life: Life::Stopped,
                    waiter: Waiter::Dead,
                    notify: false,
                };
            }
            Event::Escalate(e) => self.escalate(e)?,
            Event::Alarm(e) => self.alarm(e)?,
            Event::Recover => self.recover()?,
            Event::Nudge => {
                // The retry stays open in every mode while a binding event is unread: rollback and a
                // lost GO queue one wake, and that wake can be lost.
                let owed = self.mode == Mode::Active || self.unread_binding();
                if !owed || self.has(Defect::NoSupervisor) || !self.nudge_target() {
                    return Err(Denied::Guard);
                }
                // Each nudge gives the wake one more supervisor pass.
                (self.session.notify, self.asleep_age) = (true, 0);
            }
            Event::StartShadow if self.mode == Mode::Off => self.mode = Mode::Shadow,
            Event::SignOffShadow if self.mode == Mode::Shadow => self.gates.shadow_ok = true,
            Event::DeployFence => self.gates.fence = true,
            Event::DeployRecovery => self.gates.recovery = true,
            Event::GrantGo => self.gates.go = true,
            Event::RevokeGo => {
                self.gates.go = false;
                if self.mode == Mode::Active {
                    self.force_off();
                }
            }
            Event::Activate => self.activate()?,
            Event::Rollback if self.mode != Mode::Off => self.force_off(),
            _ => return Err(Denied::Mode),
        }
        Ok(())
    }

    /// Switching on is refused while an urgent event is already past its alarm age: the role
    /// would start with a bound that is already broken.
    fn activate(&mut self) -> Result<(), Denied> {
        let g = self.gates;
        if self.mode != Mode::Shadow {
            return Err(Denied::Mode);
        }
        if !(g.fence && g.recovery && g.go && g.shadow_ok)
            && !self.has(Defect::ActivateWithoutGates)
        {
            return Err(Denied::Gate);
        }
        let overdue = self.events.iter().any(|e| {
            e.open() && !e.alarmed && e.age >= Params::ticks(self.p.budget(e.class).alarm_secs)
        });
        if overdue && !self.has(Defect::ActivateOverdue) {
            return Err(Denied::Overdue);
        }
        // What is pending now is held to its deadline from here on, in every mode.
        for e in self.events.iter_mut().filter(|e| e.open()) {
            e.binding = true;
        }
        self.mode = Mode::Active;
        Ok(())
    }

    pub(crate) fn source(s: u8) -> Result<usize, Denied> {
        if usize::from(s) < MAX_SOURCES {
            Ok(usize::from(s))
        } else {
            Err(Denied::Index)
        }
    }

    /// The session is inside a turn.
    fn working(&self) -> Result<(), Denied> {
        if self.session.life == Life::Active {
            Ok(())
        } else {
            Err(Denied::Guard)
        }
    }

    /// Back to today's behaviour: every unread event is handed to the existing wake path.
    fn force_off(&mut self) {
        self.mode = Mode::Off;
        self.gates.shadow_ok = false;
        let mut open = false;
        for e in self.events.iter_mut().filter(|e| e.open()) {
            (e.escalated, open) = (true, true);
        }
        if self.session.waiter == Waiter::Armed {
            self.session.waiter = Waiter::Dead;
        }
        if open {
            self.wake();
        }
    }
}
