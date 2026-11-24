use crate::*;

impl Model {
    /// Apply one atomic transition. A refused event leaves the model exactly as it was.
    pub fn apply(&mut self, event: Event) -> Result<Outcome, Denied> {
        let mut next = self.clone();
        let outcome = next.step(event)?;
        next.settle();
        *self = next;
        Ok(outcome)
    }

    fn step(&mut self, event: Event) -> Result<Outcome, Denied> {
        match event {
            Event::Tick => self.tick()?,
            Event::Occur(e) => self.occur(e)?,
            Event::Scan(s) => self.scan(s)?,
            Event::Hang(s) => self.hung[Self::source(s)?] = true,
            Event::Heal(s) => self.hung[Self::source(s)?] = false,
            Event::WaiterFire(i) => {
                let ix = self.idx(i)?;
                if !self.fire_enabled(ix) {
                    return Err(Denied::Guard);
                }
                self.fire(ix);
            }
            Event::WindowEnd(i) => {
                let ix = self.idx(i)?;
                let s = &self.sessions[ix];
                if self.mode != Mode::Active || s.fenced || s.waiter != Waiter::Armed {
                    return Err(Denied::Guard);
                }
                self.fire(ix);
            }
            Event::WaiterDie(i) => {
                let ix = self.idx(i)?;
                if self.sessions[ix].waiter != Waiter::Armed {
                    return Err(Denied::Guard);
                }
                self.sessions[ix].waiter = Waiter::Dead;
            }
            Event::BeginTurn(i) => {
                let ix = self.idx(i)?;
                let s = &mut self.sessions[ix];
                if s.life != Life::Idle || !s.notify {
                    return Err(Denied::Guard);
                }
                (s.life, s.notify, s.blocks, s.ready) = (Life::Active, false, 0, false);
            }
            Event::Arm(i) => {
                let ix = self.working(i)?;
                if self.mode != Mode::Active {
                    return Err(Denied::Mode);
                }
                if self.sessions[ix].fenced {
                    return Err(Denied::Fence);
                }
                if self.sessions[ix].waiter == Waiter::Armed {
                    return Err(Denied::Guard);
                }
                self.sessions[ix].waiter = Waiter::Armed;
            }
            Event::Receive(i) => {
                let ix = self.working(i)?;
                if self.sessions[ix].fenced {
                    return Err(Denied::Fence);
                }
                self.receive();
            }
            Event::EndTurn(i) => return self.end_turn(i),
            Event::ToolUse(i) => {
                self.working(i)?;
                if self.has(Defect::LaterToolCallHandles) {
                    let me = Home::Session(i);
                    for m in self.msgs.iter_mut().filter(|m| m.home == me && m.read) {
                        m.record = true;
                    }
                }
            }
            Event::ArmCron(i) => {
                let ix = self.idx(i)?;
                if !self.sessions[ix].live() {
                    return Err(Denied::Guard);
                }
                if self.mode == Mode::Active {
                    return Err(Denied::Cron);
                }
                self.sessions[ix].cron = true;
            }
            Event::ClearCron(i) => {
                let ix = self.idx(i)?;
                self.sessions[ix].cron = false;
            }
            Event::Crash(i) => {
                let ix = self.idx(i)?;
                if !self.sessions[ix].live() {
                    return Err(Denied::Guard);
                }
                self.stop_session(i);
            }
            Event::MarkReady(i) => {
                let ix = self.working(i)?;
                if self.sessions[ix].fenced {
                    return Err(Denied::Fence);
                }
                self.sessions[ix].ready = true;
            }
            Event::SendMsg(m) => self.send(m)?,
            Event::ReadMsg { msg, session } => {
                let im = self.msg_actor(msg, session)?;
                if self.msgs[im].read {
                    return Err(Denied::Guard);
                }
                self.msgs[im].read = true;
            }
            Event::HandleMsg { msg, session } => {
                let im = self.msg_actor(msg, session)?;
                if !self.msgs[im].read || self.msgs[im].record {
                    return Err(Denied::Guard);
                }
                self.msgs[im].acted = true;
                self.msgs[im].record = true;
            }
            Event::Escalate(e) => self.escalate(e)?,
            Event::Nudge => {
                if self.mode != Mode::Active
                    || self.has(Defect::NoSupervisor)
                    || !self.nudge_target()
                {
                    return Err(Denied::Guard);
                }
                self.sessions[usize::from(self.adopted)].notify = true;
            }
            Event::BeginRecycle => self.begin_recycle()?,
            Event::Confirm => self.confirm()?,
            Event::AbortRecycle => self.abort_recycle()?,
            Event::Adopt => self.adopt()?,
            Event::StopOk => self.stop_ok()?,
            Event::StopFail => match self.recycle {
                Recycle::StopPending { .. } => return Ok(Outcome::StopFailed),
                _ => return Err(Denied::Guard),
            },
            Event::StartShadow if self.mode == Mode::Off => self.mode = Mode::Shadow,
            Event::SignOffShadow if self.mode == Mode::Shadow => self.gates.shadow_ok = true,
            Event::DeployFence(f) if f != Fence::Absent => {
                if self.gates.fence != Fence::Deployed {
                    self.gates.fence = f;
                }
            }
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
        Ok(Outcome::Applied)
    }

    fn activate(&mut self) -> Result<(), Denied> {
        let g = self.gates;
        let gated = g.fence != Fence::Absent && g.recovery && g.go && g.shadow_ok;
        if self.mode != Mode::Shadow {
            return Err(Denied::Mode);
        }
        if !gated && !self.has(Defect::ActivateWithoutGates) {
            return Err(Denied::Gate);
        }
        if self.sessions.iter().any(|s| s.live() && s.cron) {
            return Err(Denied::Cron);
        }
        self.mode = Mode::Active;
        Ok(())
    }

    pub(crate) fn idx(&self, i: u8) -> Result<usize, Denied> {
        if i < self.p.sessions {
            Ok(usize::from(i))
        } else {
            Err(Denied::Index)
        }
    }

    pub(crate) fn source(s: u8) -> Result<usize, Denied> {
        if usize::from(s) < MAX_SOURCES {
            Ok(usize::from(s))
        } else {
            Err(Denied::Index)
        }
    }

    /// A live session inside a turn.
    pub(crate) fn working(&self, i: u8) -> Result<usize, Denied> {
        let ix = self.idx(i)?;
        if self.sessions[ix].life == Life::Active {
            Ok(ix)
        } else {
            Err(Denied::Guard)
        }
    }

    fn msg_actor(&self, msg: u8, session: u8) -> Result<usize, Denied> {
        let is = self.working(session)?;
        let im = usize::from(msg);
        if im >= self.msgs.len() {
            return Err(Denied::Index);
        }
        if self.msgs[im].home != Home::Session(session) {
            return Err(Denied::Guard);
        }
        if self.sessions[is].fenced {
            return Err(Denied::Fence);
        }
        Ok(im)
    }

    pub(crate) fn wake_adopted(&mut self) {
        let s = &mut self.sessions[usize::from(self.adopted)];
        if s.live() {
            s.notify = true;
        }
    }

    pub(crate) fn stop_session(&mut self, ix: u8) {
        let s = &mut self.sessions[usize::from(ix)];
        (s.life, s.waiter, s.notify) = (Life::Stopped, Waiter::Dead, false);
    }

    fn send(&mut self, m: u8) -> Result<(), Denied> {
        let im = usize::from(m);
        if self.msgs.get(im).ok_or(Denied::Index)?.home != Home::Future {
            return Err(Denied::Guard);
        }
        if self.sessions[usize::from(self.adopted)].live() {
            self.msgs[im].home = Home::Session(self.adopted);
            self.wake_adopted();
        } else {
            self.msgs[im].home = Home::Inbox;
        }
        Ok(())
    }

    /// The Stop hook keeps a current session's turn open until its wait is armed and the
    /// digest is received, but only up to its cap: it is a help, not a proof.
    fn end_turn(&mut self, i: u8) -> Result<Outcome, Denied> {
        let ix = self.working(i)?;
        let pending = self.events.iter().any(|e| e.stage == Stage::Detected);
        let cap = self.p.stop_cap;
        let hook = self.mode == Mode::Active && !self.sessions[ix].fenced;
        let s = &mut self.sessions[ix];
        if hook && (s.waiter != Waiter::Armed || pending) && s.blocks < cap {
            s.blocks += 1;
            return Ok(Outcome::Blocked);
        }
        (s.life, s.blocks) = (Life::Idle, 0);
        Ok(Outcome::Applied)
    }
}
