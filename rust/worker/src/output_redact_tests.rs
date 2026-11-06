use super::*;

#[test]
fn redacted_tail_normalizes_crlf_before_line_and_byte_bounds() {
    let tail = pg_worker_core::redact::redacted_tail;
    for newline in ["\n", "\r\n"] {
        let input = format!("discard{newline}middle{newline}énd{newline}");
        assert_eq!(tail(&input, 2, 100), "middle\nénd");
        // Three bytes cuts inside é; the tail must advance to the next
        // UTF-8 boundary rather than retain a partial code point.
        assert_eq!(tail(&input, 2, 3), "nd");
    }
}

macro_rules! wrapped {
    ($name:ident, $prefix:expr, $body:expr) => {
        #[test]
        fn $name() {
            let prefix = $prefix;
            let body: String = $body;
            for split in [0, 1, body.len() / 2] {
                let (left, right) = body.split_at(split);
                for newline in ["\n", "\r\n"] {
                    let text = format!("{prefix}{left}{newline}{right}\nfollowing line survives");
                    assert!(text.contains(right));
                    // The shared sk floor is one: a nonempty first line is
                    // already complete, so subsequent prose is not joined.
                    let expected = if (prefix == ["s", "k-"].concat()
                        || prefix == ["xs", "k-"].concat())
                        && split > 0
                    {
                        format!("[REDACTED]\n{right}\nfollowing line survives")
                    } else {
                        "[REDACTED]\nfollowing line survives".to_string()
                    };
                    assert_eq!(history_text(&text), expected);
                }
            }
        }
    };
}

wrapped!(wrapped_sk, ["s", "k-"].concat(), "a9".repeat(12));
wrapped!(wrapped_xsk, ["xs", "k-"].concat(), "b8".repeat(12));
wrapped!(wrapped_ghp, ["gh", "p_"].concat(), "c7".repeat(15));
wrapped!(
    wrapped_github_pat,
    ["github", "_pat_"].concat(),
    "d6".repeat(16)
);
wrapped!(wrapped_xox, ["xo", "xb-"].concat(), "e5".repeat(8));
wrapped!(wrapped_akia, ["AK", "IA"].concat(), "AB".repeat(8));
wrapped!(wrapped_aiza, ["AI", "za"].concat(), "f4".repeat(18));

#[test]
fn wrapped_token_stops_at_non_token_characters() {
    let text = format!(
        "{}{}\n{}; retained prose\nnext line",
        ["gh", "o_"].concat(),
        "abc123abc123",
        "def456def456"
    );
    assert_eq!(history_text(&text), "[REDACTED]; retained prose\nnext line");
}

#[test]
fn question_text_default_and_tags_are_redacted() {
    let secret = format!("{}{}", "sk-", "pilotseed-0123456789abcdef");
    let q = crate::question::Question {
        text: secret.clone(),
        default: Some(secret.clone()),
        deadline_s: None,
        risk_tags: vec![secret],
    };
    let json = serde_json::to_string(&question(q)).unwrap();
    assert!(!json.contains("pilotseed"), "{json}");
}

#[test]
fn review_wrap_edges() {
    let prefix = ["gh", "p_"].concat();
    for body in [
        format!("\n{}", "c7".repeat(18)),
        format!(
            "{}\n{}\n{}",
            "c7".repeat(3),
            "c7".repeat(4),
            "c7".repeat(11)
        ),
    ] {
        let input = format!("{prefix}{body}\nfollowing word");
        assert!(input.contains("c7"));
        assert_eq!(history_text(&input), "[REDACTED]\nfollowing word");
    }
    let short = format!("{prefix}a\nb");
    assert_eq!(history_text(&short), short);
    let complete = format!("{prefix}{}\nis fun", "c7".repeat(18));
    assert_eq!(history_text(&complete), "[REDACTED]\nis fun");
    let benign = ["sk", "-word\nvalue"].concat();
    assert_eq!(history_text(&benign), "[REDACTED]\nvalue");
}

#[test]
fn redaction_precedes_byte_boundary_and_questions_keep_head() {
    let secret = format!(
        "{}{}\n{}",
        ["gh", "p_"].concat(),
        "c7".repeat(4),
        "c7".repeat(14)
    );
    let text = format!("head {} {secret} {}", "é".repeat(32750), "z".repeat(65500));
    assert!(text.contains("c7"));
    let out = history_text(&text);
    assert!(!out.contains("c7"));
    assert!(out.len() <= pg_worker_core::redact::MAX_TAIL_BYTES);
    let q = crate::question::Question {
        text,
        default: None,
        deadline_s: None,
        risk_tags: vec![],
    };
    assert!(question(q).text.starts_with("head "));
}
