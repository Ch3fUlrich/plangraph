//! Existing assignment regressions, split out to keep the scrubber under its size cap.

use super::{
    tests::{env, sk},
    *,
};

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
