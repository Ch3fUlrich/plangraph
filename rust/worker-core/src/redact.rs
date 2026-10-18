//! Replace secret-shaped substrings before text is persisted or replayed.
//!
//! Gate/check tails land in workflow history and can carry credentials that
//! a child process echoed. Redaction runs BEFORE the byte cap in
//! [`redacted_tail`], so a cut can never leave half a secret behind, and the
//! cap trims on a `char` boundary so the result is always valid UTF-8.

use std::sync::OnceLock;

/// Placeholder that replaces each redacted secret.
pub const REDACTED: &str = "[REDACTED]";
/// Hard cap on a gate/check tail kept in workflow history.
pub const MAX_TAIL_BYTES: usize = 64 * 1024;

/// Compiled once and applied in order; plain alternations only, because the
/// `regex` crate has no lookaround and no backreferences.
fn patterns() -> &'static [regex::Regex] {
    static RS: OnceLock<Vec<regex::Regex>> = OnceLock::new();
    RS.get_or_init(|| {
        let specs: [&'static str; 6] = [
            // 1. Authorization/Bearer headers: keep the word "Bearer", cut the value.
            r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
            // 2. KEY=value env style: keep the name, cut the value (bare, "quoted" or 'quoted').
            r#"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Za-z0-9_]*)\s*[=:]\s*("[^"\n]*"|'[^'\n]*'|[^\s"']+)"#,
            // 3. Prefixed tokens: one alternation; every prefix is distinctive
            // enough to match on its own.
            r"\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}",
            // 4. JWT-like: three base64url segments, header always starts "eyJ".
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            // 5. Long hex — a 40-char git sha is caught too; acceptable, and shas
            // in history are decorative while leaked tokens are not.
            r"\b[0-9a-fA-F]{32,}\b",
            // 6. Long base64/url-safe blob: raw, not compiled — see [`redact`].
            r"[A-Za-z0-9+/_-]{40,}={0,2}",
        ];
        specs
            .into_iter()
            .map(|s| regex::Regex::new(s).expect("redact pattern compiles"))
            .collect()
    })
}

/// Replace token-shaped secrets in `text` with `REDACTED`.
pub fn redact(text: &str) -> String {
    let rs = patterns();
    // 1–5 in order.
    let out = rs[0].replace_all(text, "Bearer [REDACTED]").into_owned();
    let out = rs[1].replace_all(&out, "$1=[REDACTED]").into_owned();
    let out = rs[2].replace_all(&out, REDACTED).into_owned();
    let out = rs[3].replace_all(&out, REDACTED).into_owned();
    let out = rs[4].replace_all(&out, REDACTED).into_owned();
    // 6. Long blobs — but only runs mixing digits and letters, so plain long
    // words and letter-only paths survive.
    rs[5]
        .replace_all(&out, |m: &regex::Captures| {
            let t = m.get(0).unwrap().as_str();
            let has_digit = t.chars().any(|c| c.is_ascii_digit());
            let has_alpha = t.chars().any(|c| c.is_ascii_alphabetic());
            if has_digit && has_alpha {
                REDACTED.to_string()
            } else {
                t.to_string()
            }
        })
        .into_owned()
}

/// Last `max_lines` lines of `text`, redacted, then capped to `max_bytes`
/// bytes (keep the END, cut on a char boundary).
///
/// Order matters: lines -> redact -> byte cap. A cut can never leave half a
/// secret, because secrets are already replaced.
pub fn redacted_tail(text: &str, max_lines: usize, max_bytes: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    let kept = lines[lines.len().saturating_sub(max_lines)..].join("\n");
    let mut s = redact(&kept);
    if s.len() > max_bytes {
        let mut start = s.len() - max_bytes;
        while start < s.len() && !s.is_char_boundary(start) {
            start += 1;
        }
        s = s[start..].to_string();
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Fake secrets are built at runtime from fragments so that no
    /// token-shaped literal ever appears in this source file.
    fn sk() -> String {
        ["sk", "-", &"a".repeat(24)].concat()
    }
    fn gh() -> String {
        ["gh", "p_", &["b1".repeat(15), "z"].concat()].concat()
    }
    fn gpat() -> String {
        ["github", "_pat_", &"Zq9".repeat(8)].concat()
    }
    fn slack() -> String {
        ["xo", "xb-", "1234567890-abcdef"].concat()
    }
    fn aws() -> String {
        ["AK", "IA", "ABCDEFGHIJKLMNOP"].concat()
    }
    fn gcp() -> String {
        ["AI", "za", &"Km3x9".repeat(6)].concat()
    }
    fn bearer() -> String {
        ["Bear", "er ", &"c9".repeat(12)].concat()
    }
    fn jwt() -> String {
        let part = "Ab1".repeat(4);
        [["ey", "J"].concat().as_str(), &part, ".", &part, ".", &part].concat()
    }
    fn hexblob() -> String {
        "ab12".repeat(10)
    }
    fn blob() -> String {
        ["Xk9".repeat(14), "=".repeat(2)].concat()
    }
    fn env() -> String {
        ["API_", "KEY=", "hunter22hunter22"].concat()
    }

    #[test]
    fn each_family_is_redacted() {
        for secret in [
            sk(),
            gh(),
            gpat(),
            slack(),
            aws(),
            gcp(),
            bearer(),
            jwt(),
            hexblob(),
            blob(),
        ] {
            let text = format!("launching: {secret} --verbose");
            let out = redact(&text);
            assert!(!out.contains(&secret), "{secret} survived: {out}");
            assert!(out.contains(REDACTED), "{secret} not flagged: {out}");
        }
    }

    #[test]
    fn bearer_keeps_the_word() {
        let out = redact(&["Authorization: B", "earer ", &"c9".repeat(12)].concat());
        assert!(out.contains("Bearer "), "{out}");
        assert!(out.contains(REDACTED), "{out}");
        assert!(!out.contains(&"c9".repeat(12)), "{out}");
    }

    #[test]
    fn env_style_keeps_the_name() {
        let out = redact(&["MY_SE", "CRET_TOKEN=abc123"].concat());
        assert!(
            out.contains(&["MY_SE", "CRET_TOKEN=", REDACTED].concat()),
            "{out}"
        );
        assert!(!out.contains("abc123"), "{out}");
        let api = redact(&env());
        assert!(api.contains(&["API_", "KEY=", REDACTED].concat()), "{api}");
        assert!(!api.contains("hunter22hunter22"), "{api}");
    }

    #[test]
    fn harmless_text_is_untouched() {
        for t in [
            "cargo test ... ok. 12 passed; 0 failed",
            "src/lib/foo_bar.rs",
            &"u".repeat(50),
        ] {
            assert_eq!(redact(t), t, "{t} was altered");
        }
    }

    #[test]
    fn redact_is_idempotent() {
        for x in [
            "plain log line, nothing to cut",
            &format!("run {secret} here", secret = sk()),
            &["Bear", "er ", &"c9".repeat(12)].concat(),
            &env(),
        ] {
            let once = redact(x);
            assert_eq!(redact(&once), once);
        }
    }

    #[test]
    fn tail_keeps_last_lines_and_cuts_the_secret() {
        let mut text = String::new();
        for i in 1..=200 {
            if i == 190 {
                text.push_str(&format!("line {i}: token {}\n", sk()));
            } else {
                text.push_str(&format!("line {i}\n"));
            }
        }
        let out = redacted_tail(&text, 50, MAX_TAIL_BYTES);
        assert_eq!(out.lines().count(), 50);
        assert!(!out.contains(&"a".repeat(24)), "{out}");
        assert!(out.contains(REDACTED), "{out}");
    }

    #[test]
    fn tail_byte_cap_keeps_the_end_on_a_char_boundary() {
        // "é" is 2 bytes; an odd start offset would panic on a naive slice.
        let big = "é".repeat(100_000); // 200 KB
        assert_eq!(big.len(), 200_000);
        let out = redacted_tail(&big, 1000, MAX_TAIL_BYTES);
        assert!(out.len() <= MAX_TAIL_BYTES);
        assert!(std::str::from_utf8(out.as_bytes()).is_ok());
        assert!(big.ends_with(&out));
    }

    #[test]
    fn secret_cut_off_with_the_head_is_absent() {
        let text = format!("{} {}END", sk(), "z".repeat(100_000));
        let out = redacted_tail(&text, 10, 32_768);
        assert!(!out.contains(&"a".repeat(24)), "half a secret leaked");
        assert!(out.ends_with("END"));
        assert!(out.len() <= 32_768);
    }

    #[test]
    fn determinism() {
        let input = format!(
            "a {} b {} c {}",
            bearer(),
            hexblob(),
            ["MY_SE", "CRET_TOKEN=abc123"].concat()
        );
        assert_eq!(redact(&input), redact(&input));
        assert_eq!(
            redacted_tail(&input, 100, MAX_TAIL_BYTES),
            redacted_tail(&input, 100, MAX_TAIL_BYTES)
        );
    }
}
