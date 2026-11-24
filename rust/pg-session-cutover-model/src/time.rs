use crate::*;

impl Model {
    pub(crate) fn occur(&mut self, e: u8) -> Result<(), Denied> {
        let mode = self.mode;
        let ev = self.events.get_mut(usize::from(e)).ok_or(Denied::Index)?;
        if ev.stage != Stage::Future {
            return Err(Denied::Guard);
        }
        ev.stage = Stage::Occurred;
        // A role that is not switched keeps today's wake path, which carries it at once.
        ev.escalated = mode != Mode::Active;
        if mode != Mode::Active {
            self.wake_adopted();
        }
        Ok(())
    }

    /// A scan moves events into a digest (the scan cursor). It never acknowledges them: only
    /// a session's `Receive` does (the ack cursor), so a crash replays what was not received.
    pub(crate) fn scan(&mut self, s: u8) -> Result<(), Denied> {
        let src = Self::source(s)?;
        if self.mode == Mode::Off {
            return Err(Denied::Mode);
        }
        self.since_scan[src] = 0;
        // A read that times out is visible at once; it never reads another source.
        self.blind[src] = self.hung[src];
        let scan_acks = self.has(Defect::ScanIsReceipt);
        if !self.hung[src] {
            for e in self
                .events
                .iter_mut()
                .filter(|e| usize::from(e.source) == src)
            {
                if e.stage == Stage::Occurred {
                    e.stage = if scan_acks {
                        Stage::Received
                    } else {
                        Stage::Detected
                    };
                }
            }
        }
        Ok(())
    }

    /// One receipt covers at most `digest_cap` receivable events, urgent first, oldest first;
    /// the rest stay pending for the next receipt.
    pub(crate) fn receive(&mut self) {
        let active = self.mode == Mode::Active;
        let mut ids: Vec<usize> = (0..self.events.len())
            .filter(|&i| {
                let e = &self.events[i];
                let open = matches!(e.stage, Stage::Occurred | Stage::Detected);
                open && (e.escalated || (active && e.stage == Stage::Detected))
            })
            .collect();
        ids.sort_by_key(|&i| (self.events[i].class != Class::Urgent, i));
        for i in ids.into_iter().take(usize::from(self.p.digest_cap)) {
            self.events[i].stage = Stage::Received;
            self.events[i].seen = true;
        }
    }

    pub(crate) fn fire(&mut self, ix: usize) {
        self.sessions[ix].waiter = Waiter::Fired;
        self.sessions[ix].notify = true;
    }

    pub(crate) fn fire_enabled(&self, ix: usize) -> bool {
        let s = &self.sessions[ix];
        self.mode == Mode::Active
            && !s.fenced
            && s.waiter == Waiter::Armed
            && self
                .events
                .iter()
                .any(|e| e.class == Class::Urgent && e.stage == Stage::Detected)
    }

    pub(crate) fn escalate(&mut self, e: u8) -> Result<(), Denied> {
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

    pub(crate) fn nudge_target(&self) -> bool {
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

    /// A working current session must read what was handed to it: the fallback message of a
    /// takeover, or the digest of a fired wait. Without this the bound would end at a queue.
    fn receipt_due(&self) -> bool {
        self.mode == Mode::Active
            && !self.has(Defect::FallbackNotAwaited)
            && self.sessions.iter().any(|s| {
                s.life == Life::Active
                    && !s.fenced
                    && self.events.iter().any(|e| {
                        e.class == Class::Urgent
                            && matches!(e.stage, Stage::Occurred | Stage::Detected)
                            && (e.escalated
                                || (e.stage == Stage::Detected && s.waiter == Waiter::Fired))
                    })
            })
    }

    /// An obligation that must run before time may pass.
    fn due(&self) -> bool {
        let takeover = Params::ticks(self.p.takeover_secs) + Params::ticks(self.p.supervisor_secs);
        let scan = Params::ticks(self.p.scan_secs);
        let sup = Params::ticks(self.p.supervisor_secs);
        let instant = (0..self.sessions.len()).any(|ix| {
            self.fire_enabled(ix)
                || (self.sessions[ix].life == Life::Idle && self.sessions[ix].notify)
        });
        let overdue = self.mode == Mode::Active
            && !self.has(Defect::NoTakeover)
            && self.events.iter().any(|e| {
                e.class == Class::Urgent
                    && matches!(e.stage, Stage::Occurred | Stage::Detected)
                    && !e.escalated
                    && u16::from(e.age) + 1 > u16::from(takeover)
            });
        let scans = self.mode != Mode::Off
            && self
                .since_scan
                .iter()
                .any(|s| u16::from(*s) + 1 > u16::from(scan));
        let asleep = !self.has(Defect::NoSupervisor)
            && self.unattended()
            && self.supervisor_can_act()
            && u16::from(self.asleep_age) + 1 > u16::from(sup);
        instant || self.receipt_due() || overdue || scans || asleep
    }

    pub(crate) fn tick(&mut self) -> Result<(), Denied> {
        if self.due() {
            return Err(Denied::Deadline);
        }
        let bound = Params::ticks(self.p.bound_secs).saturating_add(1);
        for e in &mut self.events {
            if matches!(e.stage, Stage::Occurred | Stage::Detected) {
                e.age = e.age.saturating_add(1).min(bound);
            }
        }
        if self.mode != Mode::Off {
            let cap = Params::ticks(self.p.scan_secs).saturating_add(1);
            self.since_scan = self.since_scan.map(|s| s.saturating_add(1).min(cap));
        }
        if self.unattended() {
            let cap = Params::ticks(self.p.supervisor_secs).saturating_add(1);
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
