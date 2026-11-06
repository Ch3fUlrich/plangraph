use super::*;

macro_rules! wrapped {
    ($name:ident, $prefix:expr, $body:expr) => {
        #[test]
        fn $name() {
            let prefix = $prefix;
            let body: String = $body;
            let (left, right) = body.split_at(body.len() / 2);
            for newline in ["\n", "\r\n"] {
                let text = format!("{prefix}{left}{newline}{right}\n!following line survives");
                assert_eq!(history_text(&text), "[REDACTED]\n!following line survives");
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
        "sk", "-abc123", "def456"
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
