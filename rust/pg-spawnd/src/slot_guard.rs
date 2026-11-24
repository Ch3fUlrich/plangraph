//! A review build slot the launch adapter allocated belongs to the run until the supervisor
//! has started it; every refusal or error on the way there removes it again.
use std::path::PathBuf;

pub(crate) struct SlotGuard(Option<PathBuf>);

impl SlotGuard {
    pub(crate) fn new(slot: Option<PathBuf>) -> Self {
        Self(slot)
    }
    /// The supervisor owns the slot from here on.
    pub(crate) fn release(&mut self) {
        self.0 = None;
    }
}

impl Drop for SlotGuard {
    fn drop(&mut self) {
        if let Some(slot) = self.0.take() {
            // Refused while a lease holds the slot: whoever holds it, or hygiene, takes it later.
            let _ = pg_worker_core::cargo_pool::remove_review_slot(&slot);
        }
    }
}
