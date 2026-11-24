use crate::*;

impl Model {
    /// Idle with a fresh handoff and card, or gone: nothing in flight can be lost by replacing it.
    pub(crate) fn quiescent(&self, ix: u8) -> bool {
        let s = &self.sessions[usize::from(ix)];
        s.life == Life::Stopped || (s.life == Life::Idle && s.ready && !s.fenced)
    }

    fn free_slot(&self) -> Option<u8> {
        (0..self.p.sessions).find(|i| self.sessions[usize::from(*i)].life == Life::Unlaunched)
    }

    pub(crate) fn recycle_possible(&self) -> bool {
        self.recycle == Recycle::Idle
            && self.gates.fence == Fence::Deployed
            && self.gates.recovery
            && self.free_slot().is_some()
            && self.quiescent(self.adopted)
    }

    pub(crate) fn begin_recycle(&mut self) -> Result<(), Denied> {
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

    pub(crate) fn confirm(&mut self) -> Result<(), Denied> {
        match self.recycle {
            Recycle::Launching { pred, succ } if self.sessions[usize::from(succ)].live() => {
                if self.mode != Mode::Active {
                    return Err(Denied::Mode);
                }
                self.recycle = Recycle::Confirmed { pred, succ };
                Ok(())
            }
            _ => Err(Denied::Guard),
        }
    }

    /// The launch is cleaned up: the successor is stopped and the predecessor stays current.
    pub(crate) fn abort_recycle(&mut self) -> Result<(), Denied> {
        match self.recycle {
            Recycle::Launching { succ, .. } | Recycle::Confirmed { succ, .. } => {
                self.stop_session(succ);
                self.recycle = Recycle::Idle;
                Ok(())
            }
            _ => Err(Denied::Guard),
        }
    }

    pub(crate) fn stop_ok(&mut self) -> Result<(), Denied> {
        match self.recycle {
            // Cleanup of an adopted recycle needs no GO: it only ends a fenced session.
            Recycle::StopPending { pred, .. } => {
                self.stop_session(pred);
                self.recycle = Recycle::Idle;
                Ok(())
            }
            _ => Err(Denied::Guard),
        }
    }

    /// The one compare-and-set adoption: it fences the predecessor, revokes its wait and carries
    /// every message without a handling record. The predecessor's wait is not carried; the
    /// successor arms its own.
    pub(crate) fn adopt(&mut self) -> Result<(), Denied> {
        let Recycle::Confirmed { pred, succ } = self.recycle else {
            return Err(Denied::Guard);
        };
        if self.mode != Mode::Active {
            return Err(Denied::Mode);
        }
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
        // Whatever urgent events wait go to the successor at once, with its card.
        let mut waiting = false;
        for e in &mut self.events {
            if e.class == Class::Urgent && matches!(e.stage, Stage::Occurred | Stage::Detected) {
                (e.escalated, waiting) = (true, true);
            }
        }
        self.sessions[usize::from(succ)].fenced = false;
        if !self.has(Defect::NoFence) {
            let old = &mut self.sessions[usize::from(pred)];
            old.fenced = true;
            if old.waiter == Waiter::Armed {
                old.waiter = Waiter::Dead;
            }
        }
        let snapshot_only = self.has(Defect::CarrySnapshotOnly);
        for m in &mut self.msgs {
            let here = m.home == Home::Inbox || m.home == Home::Session(pred);
            if here && !m.record && (!snapshot_only || m.snap) {
                (m.home, m.read) = (Home::Session(succ), false);
            }
        }
        if waiting {
            self.wake_adopted();
        }
        self.recycle = if self.sessions[usize::from(pred)].life == Life::Stopped {
            Recycle::Idle
        } else {
            Recycle::StopPending { pred, succ }
        };
        Ok(())
    }

    /// Back to today's behaviour: a launch not yet adopted is cleaned up, and every unreceived
    /// event is handed to the existing wake path.
    pub(crate) fn force_off(&mut self) {
        self.mode = Mode::Off;
        self.gates.shadow_ok = false;
        if let Recycle::Launching { succ, .. } | Recycle::Confirmed { succ, .. } = self.recycle {
            self.stop_session(succ);
            self.recycle = Recycle::Idle;
        }
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
}
