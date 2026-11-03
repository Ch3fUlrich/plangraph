//! Environment-value scrubbing before pattern redaction. Provider env names read
//! by Python from OPENCODE_CONFIG_CONTENT are deliberately not ported here.
use crate::redact::{redact, REDACTED};
use regex::Regex;
use std::{ffi::OsString, sync::OnceLock};

pub struct Scrubber(Vec<String>);
impl Scrubber {
    pub fn from_env(env: &[(OsString, OsString)]) -> Self {
        static NUMBER: OnceLock<Regex> = OnceLock::new();
        let number = NUMBER.get_or_init(|| {
            Regex::new(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$").unwrap()
        });
        let mut values = Vec::new();
        for (name, value) in env {
            let name = name.to_string_lossy().to_ascii_uppercase();
            let value = value.to_string_lossy();
            if [
                "KEY",
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "CREDENTIAL",
                "AUTH",
                "BEARER",
            ]
            .iter()
            .any(|s| name.contains(s))
                && value.chars().count() >= 8
                && !number.is_match(&value)
                && !["true", "false", "yes", "no", "on", "off"]
                    .contains(&value.to_ascii_lowercase().as_str())
            {
                values.push(value.to_string());
                {
                    values.extend(
                        value
                            .split([
                                '\n', '\r', '\u{b}', '\u{c}', '\u{85}', '\u{2028}', '\u{2029}',
                            ])
                            .filter(|s| s.chars().count() >= 8)
                            .map(str::to_owned),
                    );
                }
            }
        }
        // Byte-length ordering differs from Python's character count only in tie
        // order; longest-containing-value-first replacement remains correct.
        values.sort_by(|a, b| b.len().cmp(&a.len()).then(a.cmp(b)));
        values.dedup();
        Self(values)
    }
    pub fn scrub(&self, text: &str) -> String {
        let mut text = text.to_owned();
        for value in &self.0 {
            text = text.replace(value, REDACTED);
        }
        redact(&text)
    }
}
