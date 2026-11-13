//! One token-body policy for ordinary and wrapped prefixed secrets.
//!
//! sk, gh*, github_pat, and xox bodies accept ASCII alphanumerics, `_`, and
//! `-`; AIza accepts the same alphabet, while AKIA accepts uppercase and digits.
//! Plain and wrapped paths share each family's minimum body length below.
//! LF/CRLF continuations consume the full contiguous body on each joined line;
//! after the floor, whole allowed-byte lines are consumed, including words.
//! Lines containing disallowed bytes survive. Joining stops at MAX_WRAPPED_BODY.
//! Below-floor bodies survive; AKIA retains its exact plain length and boundary.
pub(crate) const MAX_WRAPPED_BODY: usize = 4096;

pub(crate) struct TokenSpec {
    pub prefix: &'static str,
    pub body: &'static str,
    pub floor: usize,
    pub exact: bool,
}

impl TokenSpec {
    pub(crate) fn wrapped_limit(&self) -> usize {
        MAX_WRAPPED_BODY
    }
}

pub(crate) const SPECS: &[TokenSpec] = &[
    TokenSpec {
        prefix: "x?sk-",
        body: "[A-Za-z0-9_-]",
        floor: 1,
        exact: false,
    },
    TokenSpec {
        prefix: "gh[pousr]_",
        body: "[A-Za-z0-9_-]",
        floor: 20,
        exact: false,
    },
    TokenSpec {
        prefix: "github_pat_",
        body: "[A-Za-z0-9_-]",
        floor: 20,
        exact: false,
    },
    TokenSpec {
        prefix: "xox[abprs]-",
        body: "[A-Za-z0-9_-]",
        floor: 10,
        exact: false,
    },
    TokenSpec {
        prefix: "AKIA",
        body: "[0-9A-Z]",
        floor: 16,
        exact: true,
    },
    TokenSpec {
        prefix: "AIza",
        body: "[0-9A-Za-z_-]",
        floor: 30,
        exact: false,
    },
];

pub(crate) fn pattern() -> String {
    SPECS
        .iter()
        .map(|s| {
            let suffix = if s.exact { "}\\b" } else { ",}" };
            format!(r"\b{}{}{{{}{suffix}", s.prefix, s.body, s.floor)
        })
        .collect::<Vec<_>>()
        .join("|")
}

pub(crate) fn redact_url_userinfo(text: &str) -> String {
    static URL: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    URL.get_or_init(|| {
        regex::Regex::new(r#"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:"']+:)[^\s"']*(@)"#)
            .expect("userinfo pattern compiles")
    })
    .replace_all(text, "$1[REDACTED]$2")
    .into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn github_token_body_floor_is_bound_to_gh_family() {
        let spec = &SPECS[1];
        assert_eq!(spec.prefix, "gh[pousr]_");
        assert_eq!(crate::GITHUB_TOKEN_BODY_FLOOR, spec.floor);
    }

    #[test]
    fn wrapped_xox_underscore_is_redacted() {
        let prefix = ["xo", "xb-"].concat();
        let plain = format!("{prefix}ab_cd_ef_gh; retained");
        assert_eq!(crate::redact::redact(&plain), "[REDACTED]; retained");
        for newline in ["\n", "\r\n"] {
            let input = format!("{prefix}ab_{newline}cd_ef_gh; retained");
            assert_eq!(crate::redact::redact(&input), "[REDACTED]; retained");
        }
    }

    #[test]
    fn every_family_floor_and_allowed_body_match_wrapped_and_plain() {
        let families = [
            (0, "s", "k-"),
            (0, "xs", "k-"),
            (1, "gh", "p_"),
            (1, "gh", "o_"),
            (1, "gh", "u_"),
            (1, "gh", "s_"),
            (1, "gh", "r_"),
            (2, "github", "_pat_"),
            (3, "xo", "xa-"),
            (3, "xo", "xb-"),
            (3, "xo", "xp-"),
            (3, "xo", "xr-"),
            (3, "xo", "xs-"),
            (4, "AK", "IA"),
            (5, "AI", "za"),
        ];
        for (index, left, right) in families {
            let spec = &SPECS[index];
            let prefix = [left, right].concat();
            for len in [spec.floor - 1, spec.floor] {
                let alphabet = regex::Regex::new(&format!("^{}$", spec.body)).unwrap();
                let chars: Vec<char> = "A_-9"
                    .chars()
                    .filter(|c| alphabet.is_match(&c.to_string()))
                    .collect();
                let body: String = chars.iter().cycle().take(len).collect();
                for newline in ["\n", "\r\n"] {
                    let input = format!("{prefix}{newline}{body}; retained");
                    let expected = if len == spec.floor {
                        "[REDACTED]; retained"
                    } else {
                        &input
                    };
                    assert_eq!(crate::redact::redact(&input), expected, "family {index}");
                    assert_eq!(
                        crate::redact::redact_source(&input),
                        expected,
                        "family {index}"
                    );
                }
                let input = format!("{prefix}{body}; retained");
                let expected = if len == spec.floor {
                    "[REDACTED]; retained"
                } else {
                    &input
                };
                assert_eq!(crate::redact::redact(&input), expected, "family {index}");
                assert_eq!(
                    crate::redact::redact_source(&input),
                    expected,
                    "family {index}"
                );
            }
        }
    }
}
