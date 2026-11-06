use super::*;
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
