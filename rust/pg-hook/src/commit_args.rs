//! The options of the `git commit` that runs the commit-msg hook, read as Git's option parser reads
//! them: long options may be abbreviated to a unique prefix and negated with `no-`, short options
//! cluster. Only the options that decide how Git cleans the message are kept.

#[derive(Debug, Default, PartialEq, Eq)]
pub struct CommitArgs {
    /// `--verbose` / `--no-verbose`; `None` keeps `commit.verbose`.
    pub verbose: Option<bool>,
    /// `--cleanup=<mode>` (`Some(Some)`) or `--no-cleanup` (`Some(None)`, the default mode);
    /// `None` keeps `commit.cleanup`.
    pub cleanup: Option<Option<String>>,
    /// `--edit` / `--no-edit`.
    pub edit: Option<bool>,
    /// A message came from `-m`, `-F` or `-C`.
    pub message: bool,
}

impl CommitArgs {
    /// Git opens the editor unless a message was given or `--no-edit` was.
    pub fn uses_editor(&self) -> bool {
        self.edit.unwrap_or(!self.message)
    }
}

/// Long options of `git commit` that matter here or that take a value the parser must skip.
const LONG: &[(&str, bool)] = &[
    ("verbose", false),
    ("cleanup", true),
    ("edit", false),
    ("message", true),
    ("file", true),
    ("reuse-message", true),
    ("reedit-message", true),
    ("template", true),
    ("author", true),
    ("date", true),
    ("fixup", true),
    ("squash", true),
    ("trailer", true),
    ("pathspec-from-file", true),
];

/// The option `name` stands for: an exact name, else the only option it is a prefix of.
fn resolve(name: &str) -> Option<(&'static str, bool)> {
    if let Some(option) = LONG.iter().find(|(long, _)| *long == name) {
        return Some(*option);
    }
    let mut prefixed = LONG.iter().filter(|(long, _)| long.starts_with(name));
    match (prefixed.next(), prefixed.next()) {
        (Some(option), None) if !name.is_empty() => Some(*option),
        _ => None,
    }
}

/// The options after the `commit` word of `argv` (the whole command line of the git process).
pub fn parse(argv: &[String]) -> Option<CommitArgs> {
    if argv.first()?.rsplit('/').next() != Some("git") {
        return None;
    }
    let mut args = argv[argv.iter().position(|arg| arg == "commit")? + 1..].iter();
    let mut parsed = CommitArgs::default();
    while let Some(arg) = args.next() {
        if arg == "--" {
            break;
        }
        if let Some(long) = arg.strip_prefix("--") {
            let (name, attached) = match long.split_once('=') {
                Some((name, value)) => (name, Some(value.to_owned())),
                None => (long, None),
            };
            let (negated, name) = match name.strip_prefix("no-") {
                Some(rest) if resolve(name).is_none() => (true, rest),
                _ => (false, name),
            };
            let Some((option, takes_value)) = resolve(name) else {
                continue;
            };
            let value = if negated || !takes_value {
                None
            } else {
                attached.or_else(|| args.next().cloned())
            };
            match option {
                "verbose" => parsed.verbose = Some(!negated),
                "edit" => parsed.edit = Some(!negated),
                "cleanup" => parsed.cleanup = Some(value),
                "message" | "file" | "reuse-message" => parsed.message |= !negated,
                _ => {}
            }
        } else if let Some(cluster) = arg.strip_prefix('-').filter(|c| !c.is_empty()) {
            for (index, flag) in cluster.char_indices() {
                match flag {
                    'v' => parsed.verbose = Some(true),
                    'e' => parsed.edit = Some(true),
                    'm' | 'F' | 'C' | 'c' | 't' => {
                        parsed.message |= matches!(flag, 'm' | 'F' | 'C');
                        if index + 1 == cluster.len() {
                            args.next();
                        }
                        break;
                    }
                    'S' | 'u' => break,
                    _ => {}
                }
            }
        }
    }
    Some(parsed)
}

/// The options of the parent process, when it is a readable `git commit`.
pub fn parent() -> Option<CommitArgs> {
    #[cfg(unix)]
    {
        let cmdline = std::fs::read(format!(
            "/proc/{}/cmdline",
            std::os::unix::process::parent_id()
        ))
        .ok()?;
        let argv: Vec<String> = cmdline
            .split(|b| *b == 0)
            .filter(|arg| !arg.is_empty())
            .map(|arg| String::from_utf8_lossy(arg).into_owned())
            .collect();
        parse(&argv)
    }
    #[cfg(not(unix))]
    {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(line: &str) -> CommitArgs {
        let argv: Vec<String> = line.split_whitespace().map(str::to_owned).collect();
        parse(&argv).unwrap()
    }

    #[test]
    fn long_options_abbreviate_and_negate_like_git() {
        assert_eq!(args("git commit --verb").verbose, Some(true));
        assert_eq!(args("git commit --verbose --no-verb").verbose, Some(false));
        assert_eq!(args("git commit -v --no-verbose").verbose, Some(false));
        assert_eq!(
            args("git commit --cleanup=scissors").cleanup,
            Some(Some("scissors".into()))
        );
        assert_eq!(
            args("git commit --clean verbatim").cleanup,
            Some(Some("verbatim".into()))
        );
        assert_eq!(
            args("git commit --cleanup=scissors --no-cleanup").cleanup,
            Some(None)
        );
        assert_eq!(args("git commit --no-clean").cleanup, Some(None));
        assert_eq!(args("git commit").cleanup, None);
    }

    #[test]
    fn the_editor_follows_the_command_line_not_an_environment_sentinel() {
        assert!(args("git commit").uses_editor());
        assert!(!args("git commit -m msg").uses_editor());
        assert!(args("git commit -m msg --edit").uses_editor());
        assert!(args("git commit -em msg").uses_editor());
        assert!(!args("git commit -me msg").uses_editor()); // -m takes "e" as its value
        assert!(!args("git commit --amend --no-edit").uses_editor());
        assert!(!args("git commit --mess msg").uses_editor());
        // a value that looks like an option is a value
        assert_eq!(args("git commit -m -v").verbose, None);
        assert_eq!(args("git commit -avm text").verbose, Some(true));
    }

    #[test]
    fn a_parent_that_is_not_git_commit_is_unknown() {
        assert!(parse(&["sh".to_owned(), "commit".to_owned()]).is_none());
        assert!(parse(&["git".to_owned(), "status".to_owned()]).is_none());
    }
}
