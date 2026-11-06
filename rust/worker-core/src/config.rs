//! Worker defaults.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Provider tier, cheapest first. Promotion walks this order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    Trial,
    Free,
    Subscription,
    Paid,
}

impl Tier {
    /// The next more expensive tier, if any.
    pub fn next(self) -> Option<Tier> {
        match self {
            Tier::Trial => Some(Tier::Free),
            Tier::Free => Some(Tier::Subscription),
            Tier::Subscription => Some(Tier::Paid),
            Tier::Paid => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub card_allowlist: Option<std::collections::BTreeSet<String>>,
    pub heartbeat: Duration,
    pub heartbeat_timeout: Duration,
    pub start_to_close: Duration,
    pub stall: Duration,
    /// Grace between SIGTERM and SIGKILL when stopping a child.
    pub kill_grace: Duration,
    pub check_tail_lines: usize,
    /// Attempts allowed per leg tier before promotion or parking.
    pub retries_per_tier: u32,
    /// Tier promotions allowed per card.
    pub promotions: u32,
    /// First wait / backoff step; doubles each time.
    pub wait_base: Duration,
    pub wait_cap: Duration,
    pub backoff_base: Duration,
    /// Questions a card may have answered; the next `needs_input` parks it.
    /// Asking takes no retry budget, so this is what bounds a card that keeps
    /// asking.
    pub max_question_rounds: u32,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            card_allowlist: None,
            heartbeat: Duration::from_secs(30),
            heartbeat_timeout: Duration::from_secs(120),
            start_to_close: Duration::from_secs(90 * 60),
            stall: Duration::from_secs(10 * 60),
            kill_grace: Duration::from_secs(10),
            check_tail_lines: 50,
            retries_per_tier: 3,
            promotions: 1,
            wait_base: Duration::from_secs(60),
            wait_cap: Duration::from_secs(24 * 3600),
            backoff_base: Duration::from_secs(30),
            max_question_rounds: 3,
        }
    }
}

impl Config {
    /// Load once at startup; never expose file contents in an error.
    pub fn with_card_allowlist(mut self, path: Option<&std::path::Path>) -> std::io::Result<Self> {
        if let Some(path) = path {
            let invalid = || {
                std::io::Error::new(std::io::ErrorKind::InvalidInput, "PG_CARD_ALLOWLIST must name an absolute readable UTF-8 file containing card keys")
            };
            if !path.is_absolute() {
                return Err(invalid());
            }
            let text = std::fs::read_to_string(path).map_err(|_| invalid())?;
            let keys: std::collections::BTreeSet<String> = text
                .lines()
                .map(str::trim)
                .filter(|s| !s.is_empty() && !s.starts_with('#'))
                .map(str::to_owned)
                .collect();
            if keys.is_empty() {
                return Err(invalid());
            }
            self.card_allowlist = Some(keys);
        }
        Ok(self)
    }

    pub fn card_allowed(&self, key: &str) -> bool {
        self.card_allowlist
            .as_ref()
            .is_none_or(|keys| keys.contains(key))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowlist_comments_and_keys() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("cards");
        std::fs::write(&file, "# lane A\n\n c1 \n").unwrap();
        let cfg = Config::default().with_card_allowlist(Some(&file)).unwrap();
        assert!(cfg.card_allowed("c1"));
        assert!(!cfg.card_allowed("c2"));
        assert!(Config::default().card_allowed("c2"));
    }

    #[test]
    fn empty_allowlist_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("cards");
        std::fs::write(&file, "# nothing\n\n").unwrap();
        assert!(Config::default().with_card_allowlist(Some(&file)).is_err());
    }

    #[test]
    fn unreadable_allowlist_fails_closed() {
        assert!(Config::default()
            .with_card_allowlist(Some(std::path::Path::new("/nonexistent/pilot/cards")))
            .is_err());
    }

    #[test]
    fn defaults_match_spec() {
        let c = Config::default();
        assert_eq!(c.heartbeat.as_secs(), 30);
        assert_eq!(c.heartbeat_timeout.as_secs(), 120);
        assert_eq!(c.start_to_close.as_secs(), 90 * 60);
        assert_eq!(c.stall.as_secs(), 600);
        assert_eq!(c.check_tail_lines, 50);
        assert_eq!((c.retries_per_tier, c.promotions), (3, 1));
        assert_eq!(c.wait_cap.as_secs(), 86_400);
        assert_eq!(c.max_question_rounds, 3);
    }

    #[test]
    fn tier_order_and_next() {
        assert!(Tier::Trial < Tier::Free && Tier::Free < Tier::Subscription);
        assert_eq!(Tier::Trial.next(), Some(Tier::Free));
        assert_eq!(Tier::Free.next(), Some(Tier::Subscription));
        assert_eq!(Tier::Subscription.next(), Some(Tier::Paid));
        assert_eq!(Tier::Paid.next(), None);
        assert_eq!(
            serde_json::to_string(&Tier::Subscription).unwrap(),
            "\"subscription\""
        );
    }
}
