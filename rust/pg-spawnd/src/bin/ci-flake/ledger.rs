//! The expiry-bound quarantine ledger (`.github/pytest-quarantine.json`), read the way tests/conftest.py reads it.
use chrono::{DateTime, Duration, FixedOffset};
use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub id: String,
    pub owner: String,
    pub starts: DateTime<FixedOffset>,
    pub expires: DateTime<FixedOffset>,
}

#[derive(Debug, PartialEq)]
pub enum Window {
    Active,
    NotStarted,
    Expired,
}

impl Entry {
    pub fn window(&self, as_of: DateTime<FixedOffset>) -> Window {
        if as_of < self.starts {
            Window::NotStarted
        } else if as_of < self.expires {
            Window::Active
        } else {
            Window::Expired
        }
    }
}

fn utc(text: &str) -> Result<DateTime<FixedOffset>, String> {
    let at = DateTime::parse_from_rfc3339(text).map_err(|e| format!("{text}: {e}"))?;
    if at.offset().local_minus_utc() != 0 {
        return Err(format!("{text}: not UTC"));
    }
    Ok(at)
}

pub fn parse_as_of(text: &str) -> Result<DateTime<FixedOffset>, String> {
    utc(text).map_err(|e| format!("as-of timestamp must be UTC: {e}"))
}

/// Every entry of the ledger; an entry with a bad owner, a bad timestamp or a window over 48 hours is an error.
pub fn parse(text: &str) -> Result<Vec<Entry>, String> {
    let map: Value = serde_json::from_str(text).map_err(|e| format!("ledger: {e}"))?;
    let map = map
        .as_object()
        .ok_or("ledger: expected a test ID mapping")?;
    let mut entries = Vec::new();
    for (id, value) in map {
        let field = |name: &str| {
            value
                .get(name)
                .and_then(Value::as_str)
                .ok_or(format!("{id}: missing {name}"))
        };
        let owner = field("owner")?;
        if !owner.starts_with("plangraph:") || owner["plangraph:".len()..].trim().is_empty() {
            return Err(format!("invalid owner or quarantine window for {id}"));
        }
        let (starts, expires) = (utc(field("starts")?)?, utc(field("expires")?)?);
        let span = expires - starts;
        if span <= Duration::zero() || span > Duration::hours(48) {
            return Err(format!("invalid owner or quarantine window for {id}"));
        }
        entries.push(Entry {
            id: id.clone(),
            owner: owner.to_string(),
            starts,
            expires,
        });
    }
    Ok(entries)
}

/// The entries whose window holds the shared run timestamp.
pub fn active(entries: &[Entry], as_of: DateTime<FixedOffset>) -> Vec<&Entry> {
    entries
        .iter()
        .filter(|e| e.window(as_of) == Window::Active)
        .collect()
}

/// A markdown table of the ledger for the gate and operator summaries: owner and expiry of every active entry.
pub fn summary(entries: &[Entry], as_of: DateTime<FixedOffset>) -> String {
    let mut out = format!("### Quarantine ledger as of {}\n\n", as_of.to_rfc3339());
    if entries.is_empty() {
        return out + "No quarantined tests.\n";
    }
    out += "| test | owner | expires | state |\n| --- | --- | --- | --- |\n";
    for e in entries {
        let state = match e.window(as_of) {
            Window::Active => {
                "active: deselected from the blocking run, runs in the quarantine run"
            }
            Window::NotStarted => "not started: blocks as normal",
            Window::Expired => "expired: blocks as normal",
        };
        out += &format!(
            "| `{}` | {} | {} | {} |\n",
            e.id,
            e.owner,
            e.expires.to_rfc3339(),
            state
        );
    }
    out
}

/// `(package, test path)` of the active Rust entries; a Rust id is `cargo:<package>:<test path>`.
pub fn cargo_tests(entries: &[Entry], as_of: DateTime<FixedOffset>) -> Vec<(String, String)> {
    active(entries, as_of)
        .into_iter()
        .filter_map(|e| e.id.strip_prefix("cargo:"))
        .filter_map(|rest| rest.split_once(':'))
        .map(|(package, test)| (package.to_string(), test.to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    const LEDGER: &str = r#"{
      "tests/test_a.py::test_x": {"owner": "plangraph:FIX-A", "starts": "2026-11-24T00:00:00+00:00", "expires": "2026-11-25T00:00:00+00:00"},
      "tests/test_b.py::test_y": {"owner": "plangraph:FIX-B", "starts": "2026-11-20T00:00:00Z", "expires": "2026-11-21T00:00:00Z"},
      "cargo:pg-spawnd:mod::case": {"owner": "plangraph:FIX-C", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-25T12:00:00Z"}
    }"#;

    fn at(text: &str) -> DateTime<FixedOffset> {
        parse_as_of(text).unwrap()
    }

    #[test]
    fn active_entries_keep_their_owner_and_expired_ones_return_to_blocking() {
        let entries = parse(LEDGER).unwrap();
        let now = at("2026-11-24T12:00:00+00:00");
        let ids: Vec<_> = active(&entries, now)
            .iter()
            .map(|e| e.id.as_str())
            .collect();
        assert_eq!(
            ids,
            ["tests/test_a.py::test_x", "cargo:pg-spawnd:mod::case"]
        );
        let text = summary(&entries, now);
        assert!(text.contains(
            "| `tests/test_a.py::test_x` | plangraph:FIX-A | 2026-11-25T00:00:00+00:00 | active"
        ));
        assert!(text.contains(
            "| `tests/test_b.py::test_y` | plangraph:FIX-B | 2026-11-21T00:00:00+00:00 | expired"
        ));
        let later = at("2026-11-25T00:00:00Z");
        assert_eq!(
            active(&entries, later).len(),
            1,
            "the window ends at the expiry instant"
        );
    }

    #[test]
    fn rust_entries_become_package_and_test_pairs() {
        let entries = parse(LEDGER).unwrap();
        assert_eq!(
            cargo_tests(&entries, at("2026-11-24T12:00:00Z")),
            [("pg-spawnd".to_string(), "mod::case".to_string())]
        );
    }

    #[test]
    fn a_bad_owner_a_long_window_or_a_non_utc_time_is_an_error_not_a_quarantine() {
        for bad in [
            r#"{"t": {"owner": "someone", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-24T01:00:00Z"}}"#,
            r#"{"t": {"owner": "plangraph:", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-24T01:00:00Z"}}"#,
            r#"{"t": {"owner": "plangraph:X", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-26T00:00:01Z"}}"#,
            r#"{"t": {"owner": "plangraph:X", "starts": "2026-11-24T00:00:00Z", "expires": "2026-11-24T00:00:00Z"}}"#,
            r#"{"t": {"owner": "plangraph:X", "starts": "2026-11-24T00:00:00+01:00", "expires": "2026-11-24T05:00:00+01:00"}}"#,
            r#"["not", "a", "map"]"#,
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
        assert!(parse_as_of("2026-11-24T12:00:00+02:00").is_err());
    }

    #[test]
    fn the_empty_ledger_has_no_active_tests() {
        let entries = parse("{}").unwrap();
        assert!(summary(&entries, at("2026-11-24T00:00:00Z")).contains("No quarantined tests."));
    }
}
