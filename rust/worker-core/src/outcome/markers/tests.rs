use super::*;

pub(super) fn input<'a>(stdout: &'a str, stderr: &'a str) -> Input<'a> {
    Input {
        exit: 1,
        stdout,
        stderr,
        headers: &[],
        response_date: None,
        ts: 1000.0,
    }
}

pub(super) const FREE: &str = r#"{"type":"error","error":{"type":"provider.quota","message":"Rate limit exceeded. Please try again later.","status":429}}"#;
pub(super) const TRANSIENT: &str = r#"{"type":"error","error":{"type":"provider.error","message":"Rate limit exceeded. Please try again later.","status":429}}"#;

#[test]
fn each_marker_kind() {
    for (line, marker) in [
        ("Error: quota exceeded", Marker::Quota),
        ("Error: too many requests", Marker::RateLimit),
        ("Error: invalid api key", Marker::Auth),
        ("Error: unknown model", Marker::ModelUnavailable),
        ("Error: bad gateway", Marker::ProviderUnavailable),
    ] {
        assert_eq!(scan(&input(line, "")).unwrap().marker, marker);
    }
}

#[test]
fn structured_events_at_exit_zero() {
    for (line, marker) in [
        (FREE, Marker::Quota),
        (TRANSIENT, Marker::RateLimit),
        (
            r#"{"type":"error","error":{"status":503}}"#,
            Marker::ProviderUnavailable,
        ),
    ] {
        let mut i = input(line, "");
        i.exit = 0;
        assert_eq!(scan(&i).unwrap().marker, marker);
        i.stdout = "";
        i.stderr = line;
        assert!(scan(&i).is_none());
    }
}

#[test]
fn midline_marker_in_stdout_prose_is_unmarked_at_nonzero_exit() {
    assert!(scan(&input(
        "We discuss rate limit and quota and overloaded services",
        ""
    ))
    .is_none());
    assert!(scan(&input("", "We discuss rate limit and quota")).is_none());
}

#[test]
fn start_anchored_stderr_marker_counts() {
    for line in [
        "rate limit reached",
        "  quota exceeded",
        "HTTP 429",
        "unauthorized",
    ] {
        assert!(scan(&input("", line)).is_some());
        assert!(scan(&input(line, "")).is_none());
    }
}

#[test]
fn reset_after_suffix_alone_is_no_marker() {
    for line in [
        "(reset after 30)",
        "Error: (reset after 30)",
        "HTTP 403: forbidden",
    ] {
        assert!(scan(&input(line, line)).is_none());
    }
}

#[test]
fn priority_auth_over_quota_over_rate_limit() {
    let i = input(
        "Error: rate limit\nError: quota exceeded",
        "Error: insufficient credits",
    );
    assert_eq!(scan(&i).unwrap().marker, Marker::Auth);
    let i = input("Error: rate limit", "Error: quota exceeded");
    assert_eq!(scan(&i).unwrap().marker, Marker::Quota);
    let i = input(
        "Error: quota first\nError: quota second",
        "Error: quota third",
    );
    assert_eq!(scan(&i).unwrap().line, "Error: quota first");
    let i = input("Error: quota", "Error: overloaded\nunknown model");
    assert_eq!(scan(&i).unwrap().marker, Marker::ModelUnavailable);
}

#[test]
fn ansi_and_crlf_are_normalized() {
    let i = input("noise\r\n\x1b[31mError: quota exceeded\x1b[0m\rnext", "");
    assert_eq!(scan(&i).unwrap().line, "Error: quota exceeded");
    assert_eq!(
        scan(&input("", "\x1b]title\x07Error: rate limit"))
            .unwrap()
            .line,
        "Error: rate limit"
    );
}

#[test]
fn hit_line_cut_at_300_chars() {
    let line = format!("Error: quota {} Resets in 2h", "é".repeat(400));
    let hit = scan(&input(&line, "")).unwrap();
    assert_eq!(hit.line.chars().count(), 300);
    assert!(hit.line.ends_with('é'));
    assert!(hit.reset.is_none());
}

#[test]
fn claude_status_and_missing_status_guards() {
    for status in ["allowed", "allowed_warning"] {
        let line = format!(
            r#"{{"type":"rate_limit_event","rate_limit_info":{{"status":"{status}"}},"message":"credit usage limit"}}"#
        );
        let mut i = input(&line, "");
        i.exit = 0;
        assert!(scan(&i).is_none());
    }
    for (text, hit) in [
        ("usage limit", true),
        ("credit", true),
        ("daily limit", true),
        ("quota", false),
        ("rate limit", false),
    ] {
        let line = format!(r#"{{"type":"rate_limit_event","message":"{text}"}}"#);
        assert_eq!(scan(&input(&line, "")).is_some(), hit);
    }
}
