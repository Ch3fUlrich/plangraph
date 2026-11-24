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
            Event::Occur(e) => {
                let mode = self.mode;
                let ev = self.event_mut(e)?;
                if ev.stage != Stage::Future {
                    return Err(Denied::Guard);
                }
                ev.stage = Stage::Occurred;
                // A role that is not switched keeps today's wake path, which carries it at once.
                ev.escalated = mode != Mode::Active;
                if mode != Mode::Active {
                    self.wake_adopted();
                }
            }
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
            Event::FencedWaiterExit(i) => {
                let ix = self.idx(i)?;
                if !self.sessions[ix].fenced || self.sessions[ix].waiter != Waiter::Armed {
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
                let active = self.mode == Mode::Active;
                for e in &mut self.events {
                    let open = matches!(e.stage, Stage::Occurred | Stage::Detected);
                    if open && (e.escalated || (active && e.stage == Stage::Detected)) {
                        e.stage = Stage::Received;
                    }
                }
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
                let s = &mut self.sessions[ix];
                if !s.live() {
                    return Err(Denied::Guard);
                }
                (s.life, s.waiter, s.notify) = (Life::Stopped, Waiter::Dead, false);
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
            Event::Confirm => match self.recycle {
                Recycle::Launching { pred, succ } if self.sessions[usize::from(succ)].live() => {
                    self.recycle = Recycle::Confirmed { pred, succ };
                }
                _ => return Err(Denied::Guard),
            },
            Event::AbortRecycle => match self.recycle {
                Recycle::Launching { succ, .. } | Recycle::Confirmed { succ, .. } => {
                    self.stop_session(succ);
                    self.recycle = Recycle::Idle;
                }
                _ => return Err(Denied::Guard),
            },
            Event::Adopt => self.adopt()?,
            Event::StopOk => match self.recycle {
                Recycle::StopPending { pred, .. } => {
                    self.stop_session(pred);
                    self.recycle = Recycle::Idle;
                }
                _ => return Err(Denied::Guard),
            },
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
            Event::Activate => {
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
            }
            Event::Rollback if self.mode != Mode::Off => self.force_off(),
            _ => return Err(Denied::Mode),
        }
        Ok(Outcome::Applied)
    }

    fn idx(&self, i: u8) -> Result<usize, Denied> {
        if i < self.p.sessions {
            Ok(usize::from(i))
        } else {
            Err(Denied::Index)
        }
    }

    fn source(s: u8) -> Result<usize, Denied> {
        if usize::from(s) < MAX_SOURCES {
            Ok(usize::from(s))
        } else {
            Err(Denied::Index)
        }
    }

    fn event_mut(&mut self, e: u8) -> Result<&mut Evt, Denied> {
        self.events.get_mut(usize::from(e)).ok_or(Denied::Index)
    }

    /// A live session inside a turn.
    fn working(&self, i: u8) -> Result<usize, Denied> {
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

    fn wake_adopted(&mut self) {
        let s = &mut self.sessions[usize::from(self.adopted)];
        if s.live() {
            s.notify = true;
        }
    }

    fn fire(&mut self, ix: usize) {
        self.sessions[ix].waiter = Waiter::Fired;
        self.sessions[ix].notify = true;
    }

    fn fire_enabled(&self, ix: usize) -> bool {
        let s = &self.sessions[ix];
        self.mode == Mode::Active
            && !s.fenced
            && s.waiter == Waiter::Armed
            && self
                .events
                .iter()
                .any(|e| e.class == Class::Urgent && e.stage == Stage::Detected)
    }

    fn stop_session(&mut self, ix: u8) {
        let s = &mut self.sessions[usize::from(ix)];
        (s.life, s.waiter, s.notify) = (Life::Stopped, Waiter::Dead, false);
    }

    fn scan(&mut self, s: u8) -> Result<(), Denied> {
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

    fn escalate(&mut self, e: u8) -> Result<(), Denied> {
        let takeover = Params::ticks(self.p.takeover_secs);
        let ev = *self.events.get(usize::from(e)).ok_or(Denied::Index)?;
        let open = matches!(ev.stage, Stage::Occurred | Stage::Detected);
        if self.mode != Mode::Active
            || self.has(Defect::NoTakeover)
            || ev.class != Class::Urgent
            || !open
            || ev.escalated
            || ev.age < takeover
        {
            return Err(Denied::Guard);
        }
        self.events[usize::from(e)].escalated = true;
        self.wake_adopted();
        Ok(())
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

    /// Idle with a fresh handoff and card, or gone: nothing in flight can be lost by replacing it.
    fn quiescent(&self, ix: u8) -> bool {
        let s = &self.sessions[usize::from(ix)];
        s.life == Life::Stopped || (s.life == Life::Idle && s.ready && !s.fenced)
    }

    fn free_slot(&self) -> Option<u8> {
        (0..self.p.sessions).find(|i| self.sessions[usize::from(*i)].life == Life::Unlaunched)
    }

    fn recycle_possible(&self) -> bool {
        self.recycle == Recycle::Idle
            && self.gates.fence == Fence::Deployed
            && self.gates.recovery
            && self.free_slot().is_some()
            && self.quiescent(self.adopted)
    }

    fn begin_recycle(&mut self) -> Result<(), Denied> {
        let pred = self.adopted;
        if self.mode != Mode::Active || self.recycle != Recycle::Idle {
            return Err(Denied::Mode);
        }
        if self.gates.fence != Fence::Deployed || !self.gates.recovery {
            return Err(Denied::Gate);
        }
        if !self.quiescent(pred) && !self.has(Defect::RecycleWhileActive) {
            // A pending wake or a missing receipt never ends a session that is working.
            let working = self.sessions[usize::from(pred)].life == Life::Active;
            return Err(if working {
                Denied::Active
            } else {
                Denied::Guard
            });
        }
        let succ = self.free_slot().ok_or(Denied::Bound)?;
        self.sessions[usize::from(succ)] = Session::new(Life::Active, true);
        self.recycle = Recycle::Launching { pred, succ };
        for m in &mut self.msgs {
            m.snap = !m.record && (m.home == Home::Inbox || m.home == Home::Session(pred));
        }
        Ok(())
    }

    /// The one compare-and-set adoption: it fences the predecessor and carries every message
    /// without a handling record. The predecessor's wait is not carried; the successor arms its own.
    fn adopt(&mut self) -> Result<(), Denied> {
        let Recycle::Confirmed { pred, succ } = self.recycle else {
            return Err(Denied::Guard);
        };
        if !self.sessions[usize::from(succ)].live() {
            return Err(Denied::Guard);
        }
        if self.gates.fence != Fence::Deployed {
            return Err(Denied::Gate);
        }
        let working = self.sessions[usize::from(pred)].life == Life::Active;
        if !self.quiescent(pred) && !self.has(Defect::RecycleWhileActive) {
            return Err(if working {
                Denied::Active
            } else {
                Denied::Guard
            });
        }
        self.adopted_over_active |= working;
        self.adopted = succ;
        self.sessions[usize::from(succ)].fenced = false;
        if !self.has(Defect::NoFence) {
            self.sessions[usize::from(pred)].fenced = true;
        }
        let snapshot_only = self.has(Defect::CarrySnapshotOnly);
        for m in &mut self.msgs {
            let here = m.home == Home::Inbox || m.home == Home::Session(pred);
            if here && !m.record && (!snapshot_only || m.snap) {
                (m.home, m.read) = (Home::Session(succ), false);
            }
        }
        self.recycle = if self.sessions[usize::from(pred)].life == Life::Stopped {
            Recycle::Idle
        } else {
            Recycle::StopPending { pred, succ }
        };
        Ok(())
    }

    /// Back to today's behaviour: every unreceived event is handed to the existing wake path.
    fn force_off(&mut self) {
        self.mode = Mode::Off;
        self.gates.shadow_ok = false;
        let mut open = false;
        for e in &mut self.events {
            if matches!(e.stage, Stage::Occurred | Stage::Detected) {
                (e.escalated, open) = (true, true);
            }
        }
        for s in &mut self.sessions {
            if s.waiter == Waiter::Armed {
                s.waiter = Waiter::Dead;
            }
        }
        if open {
            self.wake_adopted();
        }
    }

    fn nudge_target(&self) -> bool {
        let s = &self.sessions[usize::from(self.adopted)];
        s.life == Life::Idle && !s.notify && s.waiter != Waiter::Armed
    }

    /// Some session is working, armed, woken, or about to be replaced by a live successor.
    fn attended(&self) -> bool {
        let covered = match self.recycle {
            Recycle::Launching { pred, succ } | Recycle::Confirmed { pred, succ } => {
                self.sessions[usize::from(succ)].live() && self.quiescent(pred)
            }
            _ => false,
        };
        covered
            || self.sessions.iter().any(|s| {
                s.live()
                    && !s.fenced
                    && (s.life == Life::Active || s.waiter == Waiter::Armed || s.notify)
            })
    }

    pub(crate) fn unattended(&self) -> bool {
        self.mode == Mode::Active && !self.attended()
    }

    /// The supervisor has a move: nudge, recover, abort a launch or retry a stop. When it has
    /// none (every slot used) the role raises an alarm instead.
    pub(crate) fn supervisor_can_act(&self) -> bool {
        self.nudge_target() || self.recycle_possible() || self.recycle != Recycle::Idle
    }

    /// An obligation that must run before time may pass.
    fn due(&self) -> bool {
        let takeover = Params::ticks(self.p.takeover_secs) + Params::ticks(self.p.supervisor_secs);
        let scan = Params::ticks(self.p.scan_secs);
        let sup = Params::ticks(self.p.supervisor_secs);
        let instant = (0..self.sessions.len()).any(|ix| {
            let s = &self.sessions[ix];
            self.fire_enabled(ix)
                || (s.fenced && s.waiter == Waiter::Armed)
                || (s.life == Life::Idle && s.notify)
        });
        let overdue = self.mode == Mode::Active
            && !self.has(Defect::NoTakeover)
            && self.events.iter().any(|e| {
                e.class == Class::Urgent
                    && matches!(e.stage, Stage::Occurred | Stage::Detected)
                    && !e.escalated
                    && e.age + 1 > takeover
            });
        let scans = self.mode != Mode::Off && self.since_scan.iter().any(|s| s + 1 > scan);
        let asleep = !self.has(Defect::NoSupervisor)
            && self.unattended()
            && self.supervisor_can_act()
            && self.asleep_age + 1 > sup;
        instant || overdue || scans || asleep
    }

    fn tick(&mut self) -> Result<(), Denied> {
        if self.due() {
            return Err(Denied::Deadline);
        }
        let bound = Params::ticks(self.p.bound_secs) + 1;
        for e in &mut self.events {
            if matches!(e.stage, Stage::Occurred | Stage::Detected) {
                e.age = (e.age + 1).min(bound);
            }
        }
        if self.mode != Mode::Off {
            let cap = Params::ticks(self.p.scan_secs) + 1;
            self.since_scan = self.since_scan.map(|s| (s + 1).min(cap));
        }
        if self.unattended() {
            self.asleep_age = (self.asleep_age + 1).min(Params::ticks(self.p.supervisor_secs) + 1);
        }
        Ok(())
    }

    fn settle(&mut self) {
        if !self.unattended() {
            self.asleep_age = 0;
        }
        if self.mode == Mode::Off {
            self.since_scan = [0; MAX_SOURCES];
        }
    }
}
