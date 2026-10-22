//! Replace secret-shaped substrings before text is persisted or replayed.
//!
//! Gate/check tails land in workflow history and can carry credentials that
//! a child process echoed. Redaction runs BEFORE the line cut and the byte cap
//! in [`redacted_tail`], so a cut can never leave half a secret behind — not
//! even the tail half of a multi-line PEM block that straddles the cut — and
//! the cap trims on a `char` boundary so the result is always valid UTF-8.

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
        let specs: [&'static str; 9] = [
            // 1. PEM private-key blocks, complete: header through the matching
            //    footer, newlines included. Applied FIRST and to the whole
            //    text, because a multi-line secret cannot be redacted line by
            //    line — a line-wise cut would leave the half behind it.
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            // 2. A PEM header with no END after it (the block was cut off by
            //    the supervised-output cap): redact from the header to the end
            //    of the text. Second, so a complete block is always preferred.
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*",
            // 3. Authorization/Bearer headers: keep the word "Bearer", cut the value.
            r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
            // 4a. Env-style assignments: keep the credential name, cut the
            //     value. Separator whitespace is horizontal Unicode whitespace
            //     only, never CR/LF, so it never crosses a newline. Complete
            //     quoted values are preferred. Both they and the placeholder
            //     consume only a GLUED tail G = [^\s,;"'}\])&]* — directly
            //     attached, stopping at whitespace, comma, semicolon, quotes,
            //     `&`, or a closing delimiter — so the rule is idempotent.
            //     An unquoted value may not start with `}`, `]`, `)`, or `&`
            //     after either separator. An unterminated quote falls back to
            //     the rest of the line (no closing delimiter semantics). A
            //     backslash before a line feed (or CRLF) continues the value
            //     onto the next line, quoted, unquoted, or unterminated.
            r#"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Za-z0-9_]*)(["']?)([^\S\r\n]*[=:][^\S\r\n]*)("(?:[^"\\\n]|\\(?:\r?\n|.))*"[^\s,;"'}\])&]*|'(?:[^'\\\n]|\\(?:\r?\n|.))*'[^\s,;"'}\])&]*|\[REDACTED\][^\s,;"'}\])&]*|(?:\\\r?\n|[^\s"'}\])&])(?:\\\r?\n|[^\n\r}\])&])*|"(?:\\\r?\n|[^\n\r])*|'(?:\\\r?\n|[^\n\r])*)"#,
            // 4b. Same assignment rule for `=` only: after `=` an unquoted
            //     value may start with `}`, `]`, or `)` only when at least one
            //     more character follows before the next delimiter. A bare
            //     closing delimiter right after `=` is structure, not a value
            //     (and `&` is excluded, so `?K=&x=1` stays empty). Separator
            //     whitespace is horizontal Unicode whitespace only, never
            //     CR/LF. The tail class is unchanged; 4a runs first and handles
            //     the `[REDACTED]` placeholder and any glued tail after it.
            r#"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Za-z0-9_]*)(["']?)([^\S\r\n]*=[^\S\r\n]*)([}\])][^\n\r}\])&]+)"#,
            // 5. Prefixed tokens: one alternation; every prefix is distinctive
            // enough to match on its own.
            r"\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}",
            // 6. JWT-like: three base64url segments, header always starts "eyJ".
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            // 7. Long hex — a 40-char git sha is caught too; acceptable, and shas
            // in history are decorative while leaked tokens are not.
            r"\b[0-9a-fA-F]{32,}\b",
            // 8. Long base64/url-safe blob: raw, not compiled — see [`redact`].
            r"[A-Za-z0-9+/_-]{40,}={0,2}",
        ];
        specs
            .into_iter()
            .map(|s| regex::Regex::new(s).expect("redact pattern compiles"))
            .collect()
    })
}

/// Replace token-shaped secrets in `text` with `REDACTED`.
///
/// Patterns 1–2 handle multi-line PEM blocks and are applied first, to the
/// whole text; the rest are line-local. Idempotent: running an already
/// redacted text through again changes nothing.
pub fn redact(text: &str) -> String {
    let rs = patterns();
    // 1–7 in order.
    let out = rs[0].replace_all(text, REDACTED).into_owned();
    let out = rs[1].replace_all(&out, REDACTED).into_owned();
    let out = rs[2].replace_all(&out, "Bearer [REDACTED]").into_owned();
    let out = rs[3].replace_all(&out, "$1$2$3[REDACTED]").into_owned();
    let out = rs[4].replace_all(&out, "$1$2$3[REDACTED]").into_owned();
    let out = rs[5].replace_all(&out, REDACTED).into_owned();
    let out = rs[6].replace_all(&out, REDACTED).into_owned();
    let out = rs[7].replace_all(&out, REDACTED).into_owned();
    // 8. Long blobs — but only runs mixing digits and letters, so plain long
    // words and letter-only paths survive.
    rs[8]
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

/// Cap `s` to its last `max_bytes` bytes, keeping the END and cutting on a
/// `char` boundary. The final, defensive step of every tail that reaches
/// workflow history.
pub fn cap_tail_bytes(s: &str, max_bytes: usize) -> String {
    if s.len() <= max_bytes {
        return s.to_string();
    }
    let mut start = s.len() - max_bytes;
    while start < s.len() && !s.is_char_boundary(start) {
        start += 1;
    }
    s[start..].to_string()
}

/// The tail of `text` that may reach history: the WHOLE text redacted first,
/// then the last `max_lines` lines, then capped to `max_bytes` bytes (keep the
/// END, cut on a `char` boundary).
///
/// Redacting the whole text BEFORE the line cut is the point of this function:
/// a multi-line secret (a PEM block) straddling the cut is replaced as a
/// whole, so no cut can ever leave half of one behind. Redaction is
/// idempotent, so a caller may run an already-redacted tail through here
/// again to re-apply the caps.
pub fn redacted_tail(text: &str, max_lines: usize, max_bytes: usize) -> String {
    let redacted = redact(text);
    let lines: Vec<&str> = redacted.lines().collect();
    let kept = lines[lines.len().saturating_sub(max_lines)..].join("\n");
    cap_tail_bytes(&kept, max_bytes)
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
        ["gh", "p_", &["b1".repeat(15), "z".to_string()].concat()].concat()
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

    /// The PEM block used in these tests: an obviously fake header,
    /// letter-only base64 bodies — which the generic blob pattern would NOT
    /// catch (it needs digits and letters), so the PEM pattern is what must —
    /// and the matching footer.
    fn pem_block(body_lines: usize) -> Vec<String> {
        let mut v = vec!["-----BEGIN TEST PRIVATE KEY-----".to_string()];
        for _ in 0..body_lines {
            v.push("A".repeat(64));
        }
        v.push("-----END TEST PRIVATE KEY-----".to_string());
        v
    }

    #[test]
    fn pem_block_is_redacted_whole() {
        let block = pem_block(10);
        let text = format!("before\n{}\nafter", block.join("\n"));
        let out = redact(&text);
        assert!(!out.contains("PRIVATE KEY"), "{out}");
        assert!(!out.contains(&"A".repeat(64)), "{out}");
        assert!(out.contains(REDACTED), "{out}");
        // The whole multi-line block collapses into one placeholder line.
        assert_eq!(out.lines().count(), 3, "{out}");
    }

    #[test]
    fn pem_header_without_end_redacts_to_the_end_of_the_text() {
        // The supervised-output cap can cut a block after its header; what
        // follows the header must not survive without it.
        let block = pem_block(10);
        let text = format!("ok line\n{}", block.join("\n"));
        let out = redact(&text);
        assert!(out.starts_with("ok line"), "{out}");
        assert!(!out.contains("PRIVATE KEY"), "{out}");
        assert!(!out.contains(&"A".repeat(64)), "{out}");
        assert!(out.ends_with(REDACTED), "{out}");
    }

    #[test]
    fn two_pem_blocks_are_redacted_separately() {
        let block = pem_block(4);
        let text = format!("{}\nmid\n{}", block.join("\n"), block.join("\n"));
        let out = redact(&text);
        assert!(!out.contains("PRIVATE KEY"), "{out}");
        assert!(!out.contains(&"A".repeat(64)), "{out}");
        // The line between the two blocks survives; each block is one line now.
        assert_eq!(out.lines().count(), 3, "{out}");
        assert!(out.contains("\nmid\n"), "{out}");
    }

    #[test]
    fn redacted_tail_of_a_multiline_secret_across_the_cut() {
        // 100 lines with a PEM block at lines 40..57: the last-50-line cut
        // falls in the middle of the body, so cutting before redacting would
        // keep raw body lines whose header that names them is gone.
        let block = pem_block(15);
        let mut text = String::new();
        for i in 1..=39 {
            text.push_str(&format!("line {i}\n"));
        }
        text.push_str(&block.join("\n"));
        text.push('\n');
        for i in 57..=100 {
            text.push_str(&format!("line {i}\n"));
        }
        let out = redacted_tail(&text, 50, MAX_TAIL_BYTES);
        assert!(!out.contains("PRIVATE KEY"), "{out}");
        assert!(!out.contains(&"A".repeat(64)), "{out}");
        assert!(out.contains(REDACTED), "{out}");
        assert!(out.contains("line 100"), "{out}");
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
    fn quoted_json_key_keeps_name_and_drops_value() {
        let key = ["api_", "token"].concat();
        let val = ["abcd", "1234"].concat();
        let text = format!("\"{key}\": \"{val}\"");
        let out = redact(&text);
        assert_eq!(out, format!("\"{key}\": {REDACTED}"));
        assert!(!out.contains(&val), "{out}");

        let tight = format!("\"{key}\":\"{val}\"");
        let out = redact(&tight);
        assert_eq!(out, format!("\"{key}\":{REDACTED}"));
    }

    #[test]
    fn single_quoted_key_and_value() {
        let key = ["client_", "secret"].concat();
        let val = ["hunter", "22hunter22"].concat();
        let text = format!("'{key}': '{val}'");
        let out = redact(&text);
        assert_eq!(out, format!("'{key}': {REDACTED}"));
        assert!(!out.contains(&val), "{out}");
    }

    #[test]
    fn quoted_value_with_spaces_is_fully_redacted() {
        let key = ["pass", "word"].concat();
        let val = ["a ", "b ", "c"].concat();
        let text = format!("{key}: \"{val}\"");
        let out = redact(&text);
        assert_eq!(out, format!("{key}: {REDACTED}"));
        assert!(!out.contains("b c"), "{out}");

        let text = format!("{key} = '{val}'");
        let out = redact(&text);
        assert_eq!(out, format!("{key} = {REDACTED}"));
    }

    #[test]
    fn separator_forms_survive() {
        let key = ["api_", "key"].concat();
        let val = ["hunter", "22hunter22"].concat();
        let text = format!("{key}={val}\n{key}: {val}\n{key} = {val}");
        let out = redact(&text);
        assert_eq!(
            out,
            format!("{key}={REDACTED}\n{key}: {REDACTED}\n{key} = {REDACTED}")
        );
    }

    #[test]
    fn prose_with_the_word_token_is_untouched() {
        for text in [
            "the token was rotated yesterday",
            "a secret meeting",
            "token rotation is routine",
            "the password was changed",
        ] {
            assert_eq!(redact(text), text, "{text} was altered");
        }
    }

    #[test]
    fn unquoted_multiword_value_is_fully_redacted() {
        let token_key = ["TO", "KEN"].concat();
        let out = redact(&format!("{token_key}=a b c"));
        assert_eq!(out, format!("{token_key}={REDACTED}"));

        let key = ["pass", "word"].concat();
        let out = redact(&format!("{key}: v w"));
        assert_eq!(out, format!("{key}: {REDACTED}"));
    }

    #[test]
    fn export_prefix_is_kept_and_value_redacted() {
        let key = ["API_", "KEY"].concat();
        let out = redact(&format!("export {key}=x y"));
        assert_eq!(out, format!("export {key}={REDACTED}"));
    }

    #[test]
    fn structured_delimiters_end_the_value() {
        let token_key = ["to", "ken"].concat();
        let out = redact(&format!("{{{token_key}: a b}}"));
        assert_eq!(out, format!("{{{token_key}: {REDACTED}}}"));

        let pass_key = ["pass", "word"].concat();
        let out = redact(&format!("({pass_key}=x y)"));
        assert_eq!(out, format!("({pass_key}={REDACTED})"));

        let out = redact(&format!("?{token_key}=a b&next=1"));
        assert_eq!(out, format!("?{token_key}={REDACTED}&next=1"));
    }

    #[test]
    fn value_stops_at_the_end_of_the_line() {
        let key = ["to", "ken"].concat();
        let out = redact(&format!("{key}=a b c\nsecond line is untouched"));
        assert_eq!(out, format!("{key}={REDACTED}\nsecond line is untouched"));
    }

    #[test]
    fn unquoted_multiword_is_idempotent() {
        let token_key = ["to", "ken"].concat();
        let api_key = ["API_", "KEY"].concat();
        let pass_key = ["pass", "word"].concat();
        let cases = [
            format!("{token_key}=a b c"),
            format!("export {api_key}=x y"),
            format!("{{{token_key}: a b}}"),
            format!("({pass_key}=x y)"),
            format!("?{token_key}=a b&next=1"),
            format!("{token_key}={REDACTED}"),
            format!("{api_key}=abc\\\ndef"),
            format!("{api_key}=\"abc\\\ndef\""),
            format!("{api_key}=\"abc\\\ndef"),
        ];
        for x in cases {
            let once = redact(&x);
            assert_eq!(redact(&once), once, "{once}");
        }
    }

    #[test]
    fn prose_still_untouched_with_multiword_rule() {
        for text in [
            "the token was rotated yesterday",
            "a secret meeting",
            "token rotation is routine",
            "the password was changed",
        ] {
            assert_eq!(redact(text), text, "{text} was altered");
        }
    }

    #[test]
    fn quotes_inside_an_unquoted_value_do_not_end_it() {
        let key = ["API_", "KEY"].concat();
        let cases = [
            (format!("{key}=a \"b c\" d"), format!("{key}={REDACTED}")),
            (format!("{key}=pre 'x' y"), format!("{key}={REDACTED}")),
            (
                format!("{{{key}: a \"b\" c}}"),
                format!("{{{key}: {REDACTED}}}"),
            ),
        ];
        for (text, expected) in cases {
            let out = redact(&text);
            assert_eq!(out, expected, "{text}");
            assert_eq!(redact(&out), out, "{out}");
        }
    }

    #[test]
    fn seat_review_edge_cases() {
        let key = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        check(format!("{key}=\"abc def"), format!("{key}={REDACTED}"));
        check(format!("{key}='abc"), format!("{key}={REDACTED}"));
        check(
            format!("{key}=\n# rotate quarterly"),
            format!("{key}=\n# rotate quarterly"),
        );
        check(format!("{key}=)abc"), format!("{key}={REDACTED}"));
        check(format!("{key}=]abc"), format!("{key}={REDACTED}"));
        check(format!("{key}=}}abc"), format!("{key}={REDACTED}"));
        check(format!("{{{key}: }}"), format!("{{{key}: }}"));
        check(format!("url?{key}=&x=1"), format!("url?{key}=&x=1"));
        check(format!("{key}=a \"b c\" d"), format!("{key}={REDACTED}"));
        // Existing inline tests replace the quoted value whole, so the
        // placeholder is not quoted here.
        check(
            format!("{{\"{key}\": \"abc\"}}"),
            format!("{{\"{key}\": {REDACTED}}}"),
        );
    }

    #[test]
    fn placeholder_tail_and_unicode_space() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        // 4a: the placeholder alternative consumes the glued "x"; replacing
        // the whole match with one placeholder drops it.
        check(format!("{name}=[REDACTED]x"), format!("{name}={REDACTED}"));
        // 4a: the placeholder alternative consumes only a glued tail; a
        // whitespace-separated tail is left exactly as it is.
        check(
            format!("{name}=[REDACTED] tail text"),
            format!("{name}=[REDACTED] tail text"),
        );
        // 4a/4b: [^\S\r\n]* puts the NBSP in group 3, so it is kept; group 4
        // matches "secret" and is replaced.
        check(
            format!("{name}=\u{00a0}secret"),
            format!("{name}=\u{00a0}{REDACTED}"),
        );
        // No value after `=` on the first line; separator/value cannot cross
        // the newline, so the text is unchanged.
        check(
            format!("{name}=\n# rotate quarterly"),
            format!("{name}=\n# rotate quarterly"),
        );
    }

    #[test]
    fn glued_tail_is_idempotent() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        check(
            format!("{name}=\"hunter2\" --flag"),
            format!("{name}={REDACTED} --flag"),
        );
        check(format!("{name}=\"a\"b"), format!("{name}={REDACTED}"));
        check(format!("{name}=[REDACTED]x"), format!("{name}={REDACTED}"));
        check(
            format!("{name}=[REDACTED] tail text"),
            format!("{name}={REDACTED} tail text"),
        );
        check(format!("{name}=\"a\",x"), format!("{name}={REDACTED},x"));

        let json = format!("{{\"{name}\":\"v\",\"other\":1}}");
        let once = redact(&json);
        let expected = format!("{{\"{name}\":{REDACTED},\"other\":1}}");
        assert_eq!(once, expected, "{json}");
        assert!(once.contains("\"other\":1"), "{once}");
        assert_eq!(redact(&once), once, "{once}");
    }

    #[test]
    fn colon_empty_value_before_closing_brace_is_unchanged() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        // Trace: 4b is `=` only. 4a needs a non-empty value; after `:` the
        // next byte is `}` or `)`, and 4a's unquoted value branch excludes
        // both, so none of these forms match.
        check(format!("{{{name}: }}"), format!("{{{name}: }}"));
        check(format!("{{\"{name}\": }}"), format!("{{\"{name}\": }}"));
        check(format!("({name}: )"), format!("({name}: )"));
    }

    #[test]
    fn opening_quote_belongs_to_the_value_not_the_key_group() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        // Trace: in `NAME="abc"` the optional key quote after the name is
        // empty because the next byte is `=`; 4a's group 3 takes `=`, and
        // its complete quoted-value branches consume `"abc"`/`'abc'`/`"abc def"`.
        check(format!("{name}=\"abc\""), format!("{name}={REDACTED}"));
        check(format!("{name}='abc'"), format!("{name}={REDACTED}"));
        check(format!("{name}=\"abc def\""), format!("{name}={REDACTED}"));
    }

    #[test]
    fn bare_closer_after_equals_is_structure() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        // A bare closing delimiter immediately after `=` is structure; 4b
        // needs at least one character after the leading `}` / `]` / `)`.
        check(format!("({name}=)"), format!("({name}=)"));
        check(format!("{{{name}=}}"), format!("{{{name}=}}"));
        check(format!("[{name}=]"), format!("[{name}=]"));

        // 4a does not match because its unquoted value may not start with `)`;
        // 4b matches `)abc`, so the closer and value are replaced together.
        check(
            format!("x({name}=)abc"),
            ["x(", name.as_str(), "=", REDACTED].concat(),
        );
    }

    #[test]
    fn backslash_newline_continues_a_value() {
        let name = ["API_", "KEY"].concat();
        let check = |input: String, expected: String| {
            let once = redact(&input);
            assert_eq!(once, expected, "{input}");
            assert_eq!(redact(&once), once, "{once}");
        };

        check(format!("{name}=abc\\\ndef"), format!("{name}={REDACTED}"));
        check(format!("{name}=a\\\nb\\\nc"), format!("{name}={REDACTED}"));
        check(format!("{name}=abc\\\r\ndef"), format!("{name}={REDACTED}"));
        check(
            format!("{name}=\"abc\\\ndef\""),
            format!("{name}={REDACTED}"),
        );
        check(format!("{name}=\"abc\\\ndef"), format!("{name}={REDACTED}"));
        check(
            format!("{name}=abc\nnext line"),
            format!("{name}={REDACTED}\nnext line"),
        );
    }

    #[test]
    fn redact_is_idempotent() {
        let json_key = ["api_", "token"].concat();
        let json_val = ["abcd", "1234"].concat();
        let single_key = ["client_", "secret"].concat();
        let single_val = ["hunter", "22hunter22"].concat();
        let spaced_key = ["pass", "word"].concat();
        let spaced_val = ["a ", "b ", "c"].concat();
        let token_key = ["to", "ken"].concat();
        let api_key = ["API_", "KEY"].concat();
        let pass_key = ["pass", "word"].concat();
        let cases = [
            "plain log line, nothing to cut".to_string(),
            format!("run {secret} here", secret = sk()),
            ["Bear", "er ", &"c9".repeat(12)].concat(),
            env(),
            format!("\"{json_key}\": \"{json_val}\""),
            format!("'{single_key}': '{single_val}'"),
            format!("{spaced_key}: \"{spaced_val}\""),
            format!("{spaced_key} = '{spaced_val}'"),
            format!("{json_key}={json_val}"),
            format!("{token_key}=a b c"),
            format!("export {api_key}=x y"),
            format!("{{{token_key}: a b}}"),
            format!("({pass_key}=x y)"),
            format!("?{token_key}=a b&next=1"),
            format!("{token_key}={REDACTED}"),
            format!("{api_key}={REDACTED}&next=1"),
            format!("{api_key}=\"abc def"),
            format!("{api_key}=)abc"),
            format!("{api_key}=\n# rotate quarterly"),
            format!("{api_key}=a \"b c\" d"),
            format!("{api_key}=pre 'x' y"),
            format!("{{{api_key}: a \"b\" c}}"),
        ];
        for x in cases {
            let once = redact(&x);
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
