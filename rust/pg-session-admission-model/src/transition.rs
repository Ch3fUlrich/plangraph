use crate::*;

impl Model {
    /// Apply one atomic transition. A denied event preserves the record and the scope.
    pub fn apply(&mut self, event: Event) -> Result<Outcome, Denied> {
        match event {
            Event::Accept if self.phase == Phase::Missing => self.phase = Phase::Accepted,
            Event::TakeStart if self.phase == Phase::Accepted => self.phase = Phase::Starting,
            Event::BindLaunchedJob { job, process } if self.phase == Phase::Starting => {
                let identity = Identity { job, process };
                if self.launched.is_some_and(|existing| existing != identity) {
                    return Err(Denied::Identity);
                }
                self.launched = Some(identity);
            }
            Event::ObserveScope { job, process } if self.phase == Phase::Starting => {
                let identity = Identity { job, process };
                if self.scope.is_some_and(|existing| existing != identity) {
                    return Err(Denied::Scope);
                }
                self.scope = Some(identity);
            }
            Event::Revoke if self.phase == Phase::Starting => self.phase = Phase::Revoked,
            Event::FailUnclaimed
                if matches!(
                    self.phase,
                    Phase::Accepted | Phase::Starting | Phase::Revoked
                ) =>
            {
                self.phase = Phase::Failed;
            }
            Event::FailEnded if self.phase == Phase::Running => self.phase = Phase::Failed,
            Event::PreTool { caller, gate, tool } => return self.pre_tool(caller, gate, tool),
            Event::Admit { caller, gate } => {
                self.check_caller(caller, gate)?;
                if !matches!(self.phase, Phase::Starting | Phase::Running) {
                    return Err(Denied::Phase);
                }
                if self.scope != Some(caller.identity()) {
                    return Err(Denied::Scope);
                }
                self.claimed = Some(caller.identity());
                self.phase = Phase::Running;
            }
            Event::PostRead {
                caller,
                gate,
                file,
                call,
                returned,
                guidance_enabled,
            } => {
                self.check_running(caller, gate)?;
                if self.pending_reads.get(&call) != Some(&file) {
                    return Err(Denied::Delivery);
                }
                self.pending_reads.remove(&call);
                if self.files.get(file) == Some(&returned) {
                    self.marks.insert(file);
                    return Ok(Outcome::Marked {
                        guidance: guidance_enabled,
                    });
                }
                return Ok(Outcome::IgnoredRead);
            }
            Event::AdvanceConversation { job, generation }
                if self.launched.is_some_and(|identity| identity.job == job)
                    && generation > self.conversation =>
            {
                self.conversation = generation;
                self.pending_reads.clear();
                self.marks.clear();
                self.receipt = None;
            }
            Event::Receipt { caller, gate } => {
                self.check_running(caller, gate)?;
                if !self.all_files_read() {
                    return Err(Denied::Delivery);
                }
                self.receipt = Some(caller.generation);
            }
            Event::Publish {
                verified_generation,
            } if self.phase == Phase::Running => {
                if verified_generation != Some(self.conversation)
                    || verified_generation != self.receipt
                {
                    return Err(Denied::Delivery);
                }
                self.published = self.receipt;
                self.phase = Phase::Published;
            }
            Event::Ready { caller } if self.phase == Phase::Published => {
                self.check_caller(caller, Gate::Healthy)?;
                self.ready = true;
            }
            _ => return Err(Denied::Phase),
        }
        Ok(Outcome::Applied)
    }

    fn check_caller(&self, caller: Caller, gate: Gate) -> Result<(), Denied> {
        if gate != Gate::Healthy {
            return Err(Denied::Gate);
        }
        if !matches!(
            self.phase,
            Phase::Starting | Phase::Running | Phase::Published
        ) {
            return Err(Denied::Phase);
        }
        if caller.attempt != self.attempt
            || caller.root != self.root
            || self.launched != Some(caller.identity())
            || caller.generation != self.conversation
        {
            return Err(Denied::Identity);
        }
        Ok(())
    }

    fn check_running(&self, caller: Caller, gate: Gate) -> Result<(), Denied> {
        self.check_caller(caller, gate)?;
        if self.phase != Phase::Running || self.claimed != Some(caller.identity()) {
            return Err(Denied::Phase);
        }
        Ok(())
    }

    fn pre_tool(&mut self, caller: Caller, gate: Gate, tool: Tool) -> Result<Outcome, Denied> {
        self.check_caller(caller, gate)?;
        match (self.phase, tool) {
            (Phase::Starting | Phase::Running, Tool::Admit { attempt })
                if attempt == self.attempt =>
            {
                let identity = caller.identity();
                if self.scope.is_some_and(|existing| existing != identity) {
                    return Err(Denied::Scope);
                }
                self.scope = Some(identity);
                Ok(Outcome::Bootstrap)
            }
            (
                Phase::Running,
                Tool::Read {
                    file,
                    call,
                    whole: true,
                },
            ) if file < self.files.len() && !self.pending_reads.contains_key(&call) => {
                self.pending_reads.insert(call, file);
                Ok(Outcome::TaskRead)
            }
            (Phase::Running, Tool::Received { attempt }) if attempt == self.attempt => {
                Ok(Outcome::ReceiptAllowed)
            }
            (Phase::Published, Tool::ReadyOrStatus) => Ok(Outcome::ReadyOrStatus),
            (Phase::Published, _) if self.ready => Err(Denied::Ready),
            (Phase::Published, Tool::Work | Tool::ExternalWork | Tool::Read { .. }) => {
                Ok(Outcome::WorkAllowed)
            }
            _ => Err(Denied::Tool),
        }
    }
}
