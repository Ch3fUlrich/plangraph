//! The question packet a harness leaves when it needs a human, and the pure
//! decisions the workflow takes on it. No Temporal and no I/O here.
//!
//! A harness run that wants input prints one line `PG_QUESTION: {json}` and
//! stops. See `NEEDS_INPUT.md` for the whole flow.

use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Output-line prefix that marks a question packet.
pub const QUESTION_PREFIX: &str = "PG_QUESTION:";

/// Longest question, default or answer text (in characters) that is stored or
/// put into a prompt. Both come from outside the worker (an agent, a human),
/// so they are cut here and the cut is marked.
pub const MAX_TEXT_CHARS: usize = 4000;

/// Appended where [`cap_text`] cut a text.
pub const TRUNCATION_MARKER: &str = "[... truncated]";

/// `s` cut to [`MAX_TEXT_CHARS`] characters plus [`TRUNCATION_MARKER`]. A text
/// within the cap is returned unchanged; a cut never splits a character.
pub fn cap_text(s: &str) -> String {
    match s.char_indices().nth(MAX_TEXT_CHARS) {
        None => s.to_string(),
        Some((cut, _)) => format!("{}{TRUNCATION_MARKER}", &s[..cut]),
    }
}

/// The keeper's own risk tags (`RISK_TAGS` in the keeper schema). All of them
/// block a default.
const KNOWN_RISK_TAGS: [&str; 6] = [
    "auth",
    "secrets",
    "money",
    "migration",
    "public-contract",
    "live-step",
];

/// Substrings that make any (even unknown) tag spelling sensitive.
const SENSITIVE_SUBSTRINGS: [&str; 5] = ["auth", "secret", "money", "live", "public-release"];

/// One tag in canonical form: trimmed, lowercase, runs of whitespace or
/// underscores turned into a single hyphen (`"Live step"` == `"live_step"` ==
/// `"live-step"`). `None` for a tag that is empty after that.
pub fn canonical_risk_tag(tag: &str) -> Option<String> {
    let mut out = String::new();
    for c in tag.trim().to_lowercase().chars() {
        let c = if c.is_whitespace() || c == '_' {
            '-'
        } else {
            c
        };
        if c == '-' && out.ends_with('-') {
            continue;
        }
        out.push(c);
    }
    let out = out.trim_matches('-').to_string();
    (!out.is_empty()).then_some(out)
}

/// True for a canonical tag that must never get a default answer: one of the
/// keeper's known tags, or any spelling containing a sensitive substring.
pub fn is_sensitive_risk_tag(canonical: &str) -> bool {
    KNOWN_RISK_TAGS.contains(&canonical)
        || SENSITIVE_SUBSTRINGS.iter().any(|s| canonical.contains(s))
}

/// What the agent asks, with the optional fallback if nobody answers.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Question {
    pub text: String,
    #[serde(default)]
    pub default: Option<String>,
    #[serde(default)]
    pub deadline_s: Option<u64>,
    #[serde(default)]
    pub risk_tags: Vec<String>,
}

impl Question {
    /// The question as the keeper will store it. Text and default are capped
    /// ([`cap_text`]). An empty (or blank) default means "no default" and a
    /// `deadline_s` of 0 means "no deadline". A default only survives when it
    /// can be used: it needs a deadline and no sensitive risk tag (fail closed:
    /// a known keeper tag, or any spelling containing auth, secret, money, live
    /// or public-release, after canonicalisation). Tags are sent canonical.
    pub fn normalized(&self) -> Question {
        let deadline_s = self.deadline_s.filter(|s| *s > 0);
        let default = self.default.as_deref().filter(|d| !d.trim().is_empty());
        // Canonical tags first, so a spelling variant cannot dodge the check.
        let mut risk_tags: Vec<String> = Vec::new();
        for t in self.risk_tags.iter().filter_map(|t| canonical_risk_tag(t)) {
            if !risk_tags.contains(&t) {
                risk_tags.push(t);
            }
        }
        let sensitive = risk_tags.iter().any(|t| is_sensitive_risk_tag(t));
        let usable = deadline_s.is_some() && !sensitive;
        Question {
            text: cap_text(&self.text),
            default: if usable { default.map(cap_text) } else { None },
            deadline_s,
            risk_tags,
        }
    }
}

impl Question {
    /// The question with its deadline clamped to the wait cap.
    ///
    /// The keeper stores the default and applies it at the deadline, so a
    /// deadline past `wait_cap` would leave the workflow parked (no
    /// `question_timeout` recorded) while the keeper still held a default it
    /// would later take. Clamping makes the keeper's deadline and the
    /// workflow's timer the same instant. A cap under one second leaves no
    /// usable deadline, so the default is dropped with it.
    pub fn clamped_to(&self, wait_cap: Duration) -> Question {
        let mut out = self.clone();
        let cap_s = wait_cap.as_secs();
        match out.deadline_s {
            Some(s) if s > cap_s && cap_s == 0 => {
                out.deadline_s = None;
                out.default = None;
            }
            Some(s) if s > cap_s => out.deadline_s = Some(cap_s),
            _ => {}
        }
        out
    }
}

/// What the scan of a harness output found.
#[derive(Debug, Clone, PartialEq)]
pub enum Scan {
    /// No `PG_QUESTION:` line: the run did not ask anything.
    None,
    /// The last `PG_QUESTION:` line parsed.
    Asked(Question),
    /// A `PG_QUESTION:` line is there but unusable (the reason is the text).
    Malformed(String),
}

/// Find the last `PG_QUESTION:` line in `output` and parse it.
pub fn scan_output(output: &str) -> Scan {
    // Newest first: a malformed line is logged and skipped, the last
    // well-formed one wins. Only when none is usable is the (newest) defect
    // reported.
    let mut first_defect: Option<String> = None;
    for line in output
        .lines()
        .rev()
        .map(str::trim_start)
        .filter(|l| l.starts_with(QUESTION_PREFIX))
    {
        match parse_line(&line[QUESTION_PREFIX.len()..]) {
            Ok(q) => return Scan::Asked(q),
            Err(why) => {
                eprintln!("PG_QUESTION line ignored: {why}");
                first_defect.get_or_insert(why);
            }
        }
    }
    first_defect.map_or(Scan::None, Scan::Malformed)
}

fn parse_line(json: &str) -> Result<Question, String> {
    match serde_json::from_str::<Question>(json.trim()) {
        Ok(q) if q.text.trim().is_empty() => Err("question text is empty".into()),
        Ok(q) => Ok(q),
        Err(e) => Err(format!("PG_QUESTION is not a valid packet: {e}")),
    }
}

/// What the workflow does when its wait timer fires unanswered.
#[derive(Debug, Clone, PartialEq)]
pub enum OnTimer {
    /// Record `question_timeout` and continue with this default text.
    UseDefault(String),
    /// Nothing to fall back on: park and alarm.
    Park,
}

/// How long to wait for an answer and what a silent timer means.
#[derive(Debug, Clone, PartialEq)]
pub struct WaitPlan {
    pub wait: Duration,
    pub on_timer: OnTimer,
}

/// Decide the wait for `q` (already normalized), bounded by `wait_cap`.
/// A default is taken only when its deadline falls inside the cap.
pub fn plan_wait(q: &Question, wait_cap: Duration) -> WaitPlan {
    if let (Some(default), Some(s)) = (&q.default, q.deadline_s) {
        let d = Duration::from_secs(s);
        if d <= wait_cap {
            return WaitPlan {
                wait: d,
                on_timer: OnTimer::UseDefault(default.clone()),
            };
        }
    }
    WaitPlan {
        wait: wait_cap,
        on_timer: OnTimer::Park,
    }
}

/// The prompt for an attempt: the claim prompt plus every answer so far.
pub fn prompt_with_answers(prompt: &str, answers: &[String]) -> String {
    let mut out = prompt.to_string();
    for a in answers {
        out.push_str("\n\nAnswer to your question: ");
        out.push_str(&cap_text(a));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(default: Option<&str>, deadline_s: Option<u64>, tags: &[&str]) -> Question {
        Question {
            text: "which db?".into(),
            default: default.map(str::to_string),
            deadline_s,
            risk_tags: tags.iter().map(|t| t.to_string()).collect(),
        }
    }

    #[test]
    fn risk_tags_are_canonicalised_before_the_default_decision() {
        assert_eq!(canonical_risk_tag("  Live Step "), Some("live-step".into()));
        assert_eq!(canonical_risk_tag("live__step"), Some("live-step".into()));
        assert_eq!(canonical_risk_tag(" _ "), None);
        for tag in [
            "live step",
            "LIVE_STEP",
            "Live-Step",
            "secrets",
            "API Secret",
            "auth",
            "oauth_flow",
            "moneyflow",
            "go live",
            "Public Release",
            "public_release",
            "migration",
            "public contract",
        ] {
            let n = q(Some("x"), Some(5), &[tag]).normalized();
            assert_eq!(n.default, None, "{tag} must block the default");
        }
        // A benign unknown tag does not block, and is sent canonical and unique.
        let n = q(Some("x"), Some(5), &[" Docs Only ", "docs_only", " "]).normalized();
        assert_eq!(n.default.as_deref(), Some("x"));
        assert_eq!(n.risk_tags, vec!["docs-only".to_string()]);
    }

    #[test]
    fn scan_takes_the_last_question_line() {
        let out = "noise\nPG_QUESTION: {\"text\":\"a\"}\nmore\n  PG_QUESTION: {\"text\":\"b\",\"default\":\"x\",\"deadline_s\":5,\"risk_tags\":[]}\n";
        let Scan::Asked(got) = scan_output(out) else {
            panic!("expected a question");
        };
        let mut want = q(Some("x"), Some(5), &[]);
        want.text = "b".into();
        assert_eq!(got, want);
    }

    #[test]
    fn optional_fields_default_and_null_is_none() {
        let Scan::Asked(got) =
            scan_output("PG_QUESTION: {\"text\":\"t\",\"default\":null,\"deadline_s\":null}")
        else {
            panic!("expected a question");
        };
        let mut want = q(None, None, &[]);
        want.text = "t".into();
        assert_eq!(got, want);
    }

    #[test]
    fn no_line_is_none_and_bad_lines_are_malformed() {
        assert_eq!(scan_output("hello\nworld"), Scan::None);
        assert_eq!(scan_output(""), Scan::None);
        assert!(matches!(
            scan_output("PG_QUESTION: nope"),
            Scan::Malformed(_)
        ));
        assert!(matches!(scan_output("PG_QUESTION:"), Scan::Malformed(_)));
        assert!(matches!(
            scan_output("PG_QUESTION: {\"text\":\"  \"}"),
            Scan::Malformed(_)
        ));
        // A broken newer line is ignored: the last well-formed one wins.
        let out = "PG_QUESTION: {\"text\":\"ok\"}\nPG_QUESTION: {\nPG_QUESTION: {\"text\":\" \"}";
        let Scan::Asked(q) = scan_output(out) else {
            panic!("expected the well-formed line");
        };
        assert_eq!(q.text, "ok");
    }

    #[test]
    fn a_default_needs_a_deadline_and_no_risk_tag() {
        let kept = q(Some("d"), Some(9), &[]).normalized();
        assert_eq!(kept.default.as_deref(), Some("d"));
        assert_eq!(q(Some("d"), None, &[]).normalized().default, None);
        assert_eq!(q(Some("d"), Some(9), &["money"]).normalized().default, None);
    }

    #[test]
    fn wait_plan_uses_default_only_inside_the_cap() {
        let cap = Duration::from_secs(100);
        let p = plan_wait(&q(Some("d"), Some(10), &[]), cap);
        assert_eq!(p.wait, Duration::from_secs(10));
        assert_eq!(p.on_timer, OnTimer::UseDefault("d".into()));
        // Past the cap the question is clamped first (see `clamped_to`), so the
        // timer and the keeper's stored deadline are the same instant.
        let p = plan_wait(&q(Some("d"), Some(101), &[]).clamped_to(cap), cap);
        assert_eq!((p.wait, p.on_timer), (cap, OnTimer::UseDefault("d".into())));
        // No default, no deadline, or risk-tagged (after normalizing): cap and park.
        for raw in [
            q(None, Some(3), &[]),
            q(Some("d"), None, &[]),
            q(Some("d"), Some(3), &["auth"]),
        ] {
            let p = plan_wait(&raw.normalized(), cap);
            assert_eq!((p.wait, p.on_timer), (cap, OnTimer::Park));
        }
    }

    #[test]
    fn a_deadline_past_the_cap_is_clamped_to_it() {
        let cap = Duration::from_secs(100);
        let far = q(Some("d"), Some(86_400), &[]).normalized();
        let c = far.clamped_to(cap);
        assert_eq!((c.deadline_s, c.default.as_deref()), (Some(100), Some("d")));
        // Inside the cap, and no deadline at all: untouched.
        let near = q(Some("d"), Some(100), &[]).normalized();
        assert_eq!(near.clamped_to(cap), near);
        let none = q(None, None, &[]).normalized();
        assert_eq!(none.clamped_to(cap), none);
        // A cap under a second leaves no deadline, so no default either.
        let c = far.clamped_to(Duration::from_millis(500));
        assert_eq!((c.deadline_s, c.default), (None, None));
    }

    #[test]
    fn empty_default_and_zero_deadline_mean_none() {
        // A blank default is no default, even with a deadline.
        let n = q(Some(""), Some(9), &[]).normalized();
        assert_eq!((n.default, n.deadline_s), (None, Some(9)));
        assert_eq!(q(Some("  "), Some(9), &[]).normalized().default, None);
        // A zero deadline is no deadline, so the default cannot be used.
        let n = q(Some("d"), Some(0), &[]).normalized();
        assert_eq!((n.default, n.deadline_s), (None, None));
        // And the wait is then the cap, parking at the end.
        let cap = Duration::from_secs(100);
        let p = plan_wait(&q(Some("d"), Some(0), &[]).normalized(), cap);
        assert_eq!((p.wait, p.on_timer), (cap, OnTimer::Park));
    }

    #[test]
    fn untrusted_text_is_capped_with_a_marker() {
        let long = "é".repeat(MAX_TEXT_CHARS + 50);
        let c = cap_text(&long);
        assert_eq!(
            c.chars().count(),
            MAX_TEXT_CHARS + TRUNCATION_MARKER.chars().count()
        );
        assert!(c.ends_with(TRUNCATION_MARKER));
        // Exactly at the cap: untouched.
        let at = "a".repeat(MAX_TEXT_CHARS);
        assert_eq!(cap_text(&at), at);
        // The question text, the default and each answer are all bounded.
        let mut raw = q(Some(&long), Some(5), &[]);
        raw.text = long.clone();
        let n = raw.normalized();
        assert!(n.text.ends_with(TRUNCATION_MARKER));
        assert!(n.default.unwrap().ends_with(TRUNCATION_MARKER));
        let p = prompt_with_answers("do it", &[long]);
        assert!(p.ends_with(TRUNCATION_MARKER));
        assert!(p.chars().count() < MAX_TEXT_CHARS + 200);
    }

    #[test]
    fn answers_are_appended_in_order() {
        assert_eq!(prompt_with_answers("do it", &[]), "do it");
        let p = prompt_with_answers("do it", &["a".into(), "b".into()]);
        assert_eq!(
            p,
            "do it\n\nAnswer to your question: a\n\nAnswer to your question: b"
        );
    }
}
