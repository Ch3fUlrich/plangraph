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
                                '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}',
                                '\u{85}', '\u{2028}', '\u{2029}',
                            ])
                            .filter(|s| s.chars().count() >= 8)
                            .map(str::to_owned),
                    );
                }
            }
        }
        // Longest first by character count, as Python's sorted(key=len); ties by bytes.
        values.sort_by_key(|v| (std::cmp::Reverse(v.chars().count()), v.clone()));
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
#[cfg(test)]
mod tests {
    use super::Scrubber;
    fn scrubber(env: &[(&str, &str)]) -> Scrubber {
        Scrubber::from_env(
            &env.iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect::<Vec<_>>(),
        )
    }
    #[test]
    fn python_splitlines_separators_and_char_count_order() {
        for sep in ['\u{1c}', '\u{1d}', '\u{1e}'] {
            let value = format!("aaaa\n{sep}xxxxxxxx");
            let out = scrubber(&[("API_TOKEN", &value)]).scrub("<xxxxxxxx>");
            assert!(!out.contains("xxxxxxxx"), "{sep:?}: {out}");
        }
        // 8 chars (16 bytes) against 12 chars overlapping by one: Python replaces the longer first.
        let (short, long) = ("é".repeat(8), format!("é{}", "b".repeat(11)));
        let s = scrubber(&[("A_TOKEN", &short), ("B_TOKEN", &long)]);
        let out = s.scrub(&format!("{}{}", "é".repeat(7), long));
        assert!(
            !out.contains('b') && out.starts_with(&"é".repeat(7)),
            "{out}"
        );
    }
}
