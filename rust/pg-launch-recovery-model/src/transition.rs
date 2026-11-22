use crate::*;

impl Model {
    /// Apply a guarded actor event. Refusals leave state unchanged.
    pub fn apply(&mut self, id: usize, event: Event) -> Result<Outcome, Denied> {
        let mut row = *self.attempts.get(id).ok_or(Denied::Guard)?;
        let spec = row.spec;
        let local = spec.origin == spec.target;
        let others = || {
            self.attempts
                .iter()
                .enumerate()
                .filter(|(other, _)| *other != id)
                .map(|(_, row)| row)
        };
        let name_busy = |host| {
            others().any(|other| {
                other.spec.name == spec.name
                    && ((other.spec.origin == host && other.reservation != Reservation::None)
                        || (other.spec.target == host
                            && (other.phase.open() || other.phase == Phase::Published)))
            })
        };
        let seat_busy = |host| {
            others().any(|other| {
                other.spec.seat == spec.seat
                    && ((other.spec.origin == host && other.reservation.open())
                        || (other.spec.target == host && other.phase.open()))
            })
        };
        let mut answer = Outcome::Applied;
        match event {
            Event::Reserve
                if row.reservation == Reservation::None && row.phase == Phase::Missing =>
            {
                if seat_busy(spec.origin) {
                    return Err(Denied::SeatBusy);
                }
                if name_busy(spec.origin) {
                    return Err(Denied::NameTaken);
                }
                if local {
                    row.phase = Phase::Accepted;
                } else {
                    row.reservation = Reservation::Reserved;
                }
            }
            Event::Forward if row.reservation == Reservation::Reserved => {
                row.reservation = Reservation::Forwarded;
            }
            Event::Receive {
                digest,
                stale,
                name_taken,
            } if matches!(
                row.reservation,
                Reservation::Forwarded | Reservation::Mirroring | Reservation::Closed
            ) =>
            {
                if row.phase != Phase::Closed && digest != spec.digest {
                    return Err(Denied::Conflict);
                }
                if row.phase == Phase::Missing {
                    row.phase = if stale
                        || name_taken
                        || seat_busy(spec.target)
                        || name_busy(spec.target)
                    {
                        Phase::Failed
                    } else {
                        Phase::Accepted
                    };
                }
                answer = Outcome::Phase(row.phase);
            }
            Event::TakeStart if row.phase == Phase::Accepted => {
                row.phase = Phase::Starting;
                row.no_processes = false;
            }
            Event::StartChild { launcher }
                if row.phase == Phase::Starting
                    && launcher == spec.launcher
                    && row.starts == 0
                    && !row.no_processes =>
            {
                row.starts = 1;
            }
            Event::RefuseBeforeChild { launcher }
                if matches!(row.phase, Phase::Accepted | Phase::Starting)
                    && launcher == spec.launcher
                    && row.starts == 0 =>
            {
                row.phase = Phase::Failed;
            }
            Event::LaunchOutcomeUnknown if row.reservation.open() || row.phase.open() => {}
            Event::NoAttemptProcesses if row.phase.open() => row.no_processes = true,
            Event::OldEnoughToRevoke { elapsed_secs }
                if row.phase == Phase::Starting && elapsed_secs >= self.claim_window_secs =>
            {
                row.old_enough = true
            }
            Event::BindJob(job)
                if matches!(row.phase, Phase::Starting | Phase::Revoked | Phase::Failed)
                    && row.starts == 1
                    && row.bound_job.is_none_or(|bound| bound == job) =>
            {
                row.bound_job = Some(job);
                if row.phase == Phase::Revoked && row.listing == Listing::Live(job) {
                    row.observed_bound_job = true;
                }
            }
            Event::Claim(job)
                if self.admission_verified
                    && row.phase == Phase::Starting
                    && row.bound_job == Some(job) =>
            {
                row.claimed_job = Some(job);
                row.phase = Phase::Running;
            }
            Event::NewConversation(generation)
                if row.phase == Phase::Running && generation > row.generation =>
            {
                row.generation = generation;
                row.delivery = None;
            }
            Event::Delivered { job, generation }
                if row.phase == Phase::Running
                    && row.claimed_job == Some(job)
                    && generation == row.generation =>
            {
                row.delivery = Some(generation);
            }
            Event::StagePublication if row.phase == Phase::Running => row.artifacts = true,
            Event::Publish if row.can_publish(self.admission_verified) => row.publish(),
            Event::Listing {
                row: listing,
                at_secs,
            } => row.observe(listing, at_secs)?,
            Event::Boundary(boundary) if row.phase == Phase::Running => row.boundary = boundary,
            Event::CleanupJob(job)
                if matches!(row.phase, Phase::Revoked | Phase::Failed)
                    && row.bound_job == Some(job)
                    && row.listing == Listing::Live(job) =>
            {
                row.stopped_job = Some(job);
            }
            Event::StopSucceeded(job)
                if row.phase == Phase::Running && row.claimed_job == Some(job) =>
            {
                row.stopped_job = Some(job);
                row.stop_verified_by_listing = false;
            }
            Event::PidGone(job) if row.phase == Phase::Revoked && row.stopped_job == Some(job) => {
                row.pid_gone_at = row.last_listing_at;
            }
            Event::Resolve if row.reservation.open() || row.phase != Phase::Missing => {
                row.resolve(self.admission_verified);
                answer = Outcome::Phase(row.phase);
            }
            Event::StoreAnswer {
                attempt,
                target,
                phase,
            } if row.reservation == Reservation::Forwarded
                && attempt == id
                && target == spec.target
                && phase == row.phase
                && phase.terminal() =>
            {
                row.stored_answer = Some(phase);
                row.reservation = Reservation::Mirroring;
            }
            Event::MirrorAndClose if row.reservation == Reservation::Mirroring => {
                row.reservation = Reservation::Closed;
            }
            Event::Acknowledge if row.reservation == Reservation::Closed => row.ack = true,
            Event::BeginRecycle
                if row.recycle == Recycle::Idle
                    && row.phase == Phase::Missing
                    && row.reservation == Reservation::None =>
            {
                row.recycle = Recycle::Launching
            }
            Event::Confirm(job)
                if row.recycle == Recycle::Launching
                    && row.phase == Phase::Published
                    && row.claimed_job == Some(job)
                    && row.live_claim() =>
            {
                row.recycle = Recycle::Confirmed
            }
            Event::Promote
                if row.recycle == Recycle::Confirmed && row.phase == Phase::Published =>
            {
                self.promoted.insert((spec.target, spec.seat), id);
            }
            Event::MarkStopEligible if row.recycle == Recycle::Confirmed && self.stop_proof(id) => {
                row.recycle = Recycle::StopEligible;
            }
            Event::StopPredecessor
                if row.recycle == Recycle::StopEligible && self.stop_proof(id) =>
            {
                row.recycle = Recycle::Stopped;
            }
            _ => return Err(Denied::Guard),
        }
        self.attempts[id] = row;
        Ok(answer)
    }
}

impl Attempt {
    fn can_publish(&self, admission_verified: bool) -> bool {
        admission_verified
            && self.phase == Phase::Running
            && self.delivery == Some(self.generation)
            && !self.running_ended()
    }

    fn publish(&mut self) {
        self.artifacts = true;
        self.publication_proof = self.delivery;
        self.phase = Phase::Published;
    }

    fn observe(&mut self, listing: Listing, at_secs: u16) -> Result<(), Denied> {
        if !matches!(
            self.phase,
            Phase::Running | Phase::Revoked | Phase::Failed | Phase::Published
        ) || self.last_listing_at.is_some_and(|last| last >= at_secs)
        {
            return Err(Denied::Guard);
        }
        self.listing = listing;
        self.last_listing_at = Some(at_secs);
        if self.phase == Phase::Revoked
            && self
                .bound_job
                .is_some_and(|job| listing == Listing::Live(job))
        {
            self.observed_bound_job = true;
        }
        if listing == Listing::Absent {
            self.absent_since.get_or_insert(at_secs);
        } else if listing != Listing::Unreadable {
            self.absent_since = None;
        }
        self.stop_verified_by_listing = self.stopped_job.is_some()
            && (listing == Listing::Absent
                || self
                    .stopped_job
                    .is_some_and(|job| listing == Listing::Ended(job)));
        Ok(())
    }

    fn revoked_settled(&self) -> bool {
        self.listing == Listing::Absent
            && self.last_listing_at > self.revoked_listing_at
            && (!self.observed_bound_job
                || self
                    .pid_gone_at
                    .is_some_and(|at| self.last_listing_at > Some(at)))
    }

    fn running_ended(&self) -> bool {
        let two_absences = self.listing == Listing::Absent
            && self
                .absent_since
                .zip(self.last_listing_at)
                .is_some_and(|(first, last)| last - first >= 60);
        let job_ended = self
            .claimed_job
            .is_some_and(|job| self.listing == Listing::Ended(job));
        self.boundary.empty() && (two_absences || job_ended || self.stop_verified_by_listing)
    }

    fn resolve(&mut self, admission_verified: bool) {
        match self.phase {
            Phase::Missing => self.phase = Phase::Closed,
            Phase::Accepted if self.no_processes => self.phase = Phase::Failed,
            Phase::Starting if admission_verified && self.no_processes && self.old_enough => {
                self.phase = Phase::Revoked;
                self.revoked_listing_at = self.last_listing_at;
            }
            Phase::Revoked if self.revoked_settled() => self.phase = Phase::Failed,
            Phase::Running if self.running_ended() => self.phase = Phase::Failed,
            Phase::Running if self.can_publish(admission_verified) => self.publish(),
            _ => {}
        }
    }
}
