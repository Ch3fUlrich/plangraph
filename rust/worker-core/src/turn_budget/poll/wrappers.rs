//! Only transparent, literal wrappers; unknown options are not guessed.
pub(super) fn peel(command: &[String]) -> Vec<String> {
    let mut c = command;
    for _ in 0..16 {
        let program = c[0].rsplit('/').next().unwrap_or(&c[0]);
        let mut i = 1;
        let mut duration = false;
        let wrapped = matches!(program, "timeout" | "env" | "nice" | "nohup" | "command");
        if !wrapped {
            break;
        }
        while let Some(arg) = c.get(i) {
            if arg == "--" {
                i += 1;
                break;
            }
            let skip = match (program, arg.as_str()) {
                ("timeout", "-s" | "--signal" | "-k" | "--kill-after")
                | ("env", "-u" | "--unset")
                | ("nice", "-n" | "--adjustment") => 2,
                ("timeout", "--preserve-status" | "--foreground" | "-v" | "--verbose")
                | ("env", "-i" | "--ignore-environment")
                | ("command", "-p") => 1,
                ("timeout", s) if s.starts_with("--signal=") || s.starts_with("--kill-after=") => 1,
                ("env", s)
                    if s.starts_with("--unset=") || (!s.starts_with('-') && s.contains('=')) =>
                {
                    1
                }
                ("nice", s)
                    if s.starts_with("--adjustment=")
                        || s.strip_prefix('-').is_some_and(|n| {
                            !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())
                        }) =>
                {
                    1
                }
                (_, s) if s.starts_with('-') => return normalized(c),
                ("timeout", _) => {
                    duration = true;
                    i += 1;
                    break;
                }
                _ => break,
            };
            i += skip;
        }
        if program == "timeout" && !duration {
            i += 1; // duration after an explicit --
        }
        if i >= c.len() {
            break;
        }
        c = &c[i..];
    }
    normalized(c)
}

fn normalized(c: &[String]) -> Vec<String> {
    let mut c = c.to_vec();
    c[0] = c[0].rsplit('/').next().unwrap_or(&c[0]).to_owned();
    c
}

pub(super) fn script(c: &[String]) -> Option<&str> {
    if !matches!(c[0].as_str(), "bash" | "sh" | "dash" | "zsh") {
        return None;
    }
    let index = c.iter().position(|s| s == "-c" || s == "-lc")?;
    c.get(index + 1).map(String::as_str)
}
