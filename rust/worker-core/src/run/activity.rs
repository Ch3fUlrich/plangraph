//! Activity excludes terminal decorations and the opencode model banner.
use regex::Regex;
use std::{borrow::Cow, sync::OnceLock};
/// OSC ends at the first terminator, per the spec; Python's greedy BEL pattern
/// instead swallows embedded ESC-backslash and following text up to a BEL.
pub(crate) fn strip_ansi(text: &str) -> Cow<'_, str> {
    static ANSI: OnceLock<Regex> = OnceLock::new();
    ANSI.get_or_init(|| {
        Regex::new(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\](?:[^\x07\x1b]|\x1b[^\\])*?(?:\x07|\x1b\\)")
            .unwrap()
    })
    .replace_all(text, "")
}
pub fn is_activity(line: &str) -> bool {
    static BANNER: OnceLock<Regex> = OnceLock::new();
    let line = strip_ansi(line);
    let line = line.trim();
    !line.is_empty()
        && !BANNER
            .get_or_init(|| Regex::new(r"^>\s+\S+\s+·\s+\S+").unwrap())
            .is_match(line)
}
