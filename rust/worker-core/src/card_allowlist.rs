use super::*;
impl Config {
    /// Explicit unrestricted policy for test harnesses, never worker startup.
    #[cfg(all(debug_assertions, any(test, feature = "test-support")))]
    pub fn allow_all_for_tests() -> Self {
        Self {
            test_allow_all_cards: true,
            ..Self::default()
        }
    }
    /// Snapshot the environment allowlist once at startup, before sharing Config.
    /// Programmatically supplied admission policy takes precedence.
    pub fn with_card_allowlist_env(self) -> std::io::Result<Self> {
        if self.card_allowlist.is_some() {
            return Ok(self);
        }
        self.with_card_allowlist_source(|| std::env::var_os("PG_CARD_ALLOWLIST"))
    }

    /// Load startup policy from an injectable environment source.
    pub(super) fn with_card_allowlist_source(
        self,
        source: impl FnOnce() -> Option<std::ffi::OsString>,
    ) -> std::io::Result<Self> {
        if self.card_allowlist.is_some() {
            return Ok(self);
        }
        let path = source();
        self.with_card_allowlist(path.as_deref().map(std::path::Path::new))
    }
    /// Load an explicit startup policy; never expose file contents in an error.
    pub fn with_card_allowlist(mut self, path: Option<&std::path::Path>) -> std::io::Result<Self> {
        if let Some(path) = path {
            let invalid = || {
                std::io::Error::new(std::io::ErrorKind::InvalidInput, "PG_CARD_ALLOWLIST must name an absolute readable UTF-8 file containing card keys")
            };
            if !path.is_absolute() {
                return Err(invalid());
            }
            let text = std::fs::read_to_string(path).map_err(|error| {
                std::io::Error::new(
                    error.kind(),
                    format!("PG_CARD_ALLOWLIST {}: {:?}", path.display(), error.kind()),
                )
            })?;
            let keys: std::collections::BTreeSet<String> = text
                .trim_start_matches('\u{feff}')
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
        #[cfg(all(debug_assertions, any(test, feature = "test-support")))]
        let default_allowed = self.test_allow_all_cards;
        #[cfg(not(all(debug_assertions, any(test, feature = "test-support"))))]
        let default_allowed = false;
        self.card_allowlist
            .as_ref()
            .map_or(default_allowed, |keys| keys.contains(key))
    }
}
