//! Pure checks for promised source definitions; the caller owns confined I/O.

/// Return malformed or absent `path:symbol` signatures in input order.
pub fn verify(exports: &[String], read_file: &dyn Fn(&str) -> Option<String>) -> Vec<String> {
    exports
        .iter()
        .filter(|entry| {
            let Some((path, symbol)) = entry.split_once(':') else {
                return true;
            };
            let word = |c: char| c.is_alphanumeric() || c == '_';
            if path.is_empty()
                || symbol.is_empty()
                || !symbol.chars().all(word)
                || symbol.starts_with(|c: char| c.is_numeric())
                || path.starts_with(['/', '\\'])
                || path.contains(':')
                || path.split(['/', '\\']).any(|p| p == "..")
            {
                return true;
            }
            let Some(body) = read_file(path) else {
                return true;
            };
            !["def", "class", "fn", "struct", "enum", "trait"]
                .iter()
                .any(|keyword| {
                    body.match_indices(keyword).any(|(offset, _)| {
                        if body[..offset].chars().next_back().is_some_and(word) {
                            return false;
                        }
                        let rest = &body[offset + keyword.len()..];
                        if !rest.starts_with(char::is_whitespace) {
                            return false;
                        }
                        let Some(after) = rest.trim_start().strip_prefix(symbol) else {
                            return false;
                        };
                        !after.starts_with(word)
                            && (*keyword != "def" || after.trim_start().starts_with('('))
                    })
                })
        })
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn definitions() {
        for body in [
            "def run(x): pass",
            "class run:",
            "pub fn run() {}",
            "pub struct run;",
            "enum run {}",
            "trait run {}",
        ] {
            assert!(
                verify(&["src/a:run".into()], &|_| Some(body.into())).is_empty(),
                "{body}"
            );
        }
    }

    #[test]
    fn missing_file() {
        assert_eq!(verify(&["a:run".into()], &|_| None), vec!["a:run"]);
    }

    #[test]
    fn malformed_entry() {
        for entry in ["a", ":run", "a:", "a:run:extra"] {
            assert_eq!(
                verify(&[entry.into()], &|_| panic!("must not read")),
                vec![entry]
            );
        }
    }

    #[test]
    fn word_boundaries() {
        for body in [
            "fn run_it() {}",
            "struct runner;",
            "notfn run() {}",
            "def run_it(): pass",
        ] {
            assert_eq!(
                verify(&["a:run".into()], &|_| Some(body.into())),
                vec!["a:run"]
            );
        }
    }

    #[test]
    fn path_escape() {
        for entry in ["../a:run", "src/../../a:run", "/a:run", "..\\a:run"] {
            assert_eq!(
                verify(&[entry.into()], &|_| panic!("must not read")),
                vec![entry]
            );
        }
    }
}
