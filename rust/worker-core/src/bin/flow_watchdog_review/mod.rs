use super::Alert;
use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Deserialize)]
pub(super) struct Activity {
    window_seconds: u64,
    first_leg: String,
    served: BTreeMap<String, u64>,
}

impl Activity {
    pub(super) fn alert(&self) -> Option<Alert> {
        if self.window_seconds < 3600
            || self.first_leg.trim().is_empty()
            || self
                .served
                .get(&self.first_leg)
                .copied()
                .unwrap_or_default()
                > 0
            || !self.served.values().any(|&count| count > 0)
        {
            return None;
        }
        let leg = serde_json::to_string(&self.first_leg).expect("strings serialize as JSON");
        let active = format!("review-leg-unserved leg={leg}");
        Some(Alert {
            message: format!("{active} served=0 window_seconds={}", self.window_seconds),
            active,
            recipient: None,
        })
    }
}
