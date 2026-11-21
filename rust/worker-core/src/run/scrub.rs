//! Environment-value scrubbing before pattern redaction. Provider env names read
//! by Python from OPENCODE_CONFIG_CONTENT are deliberately not ported here.
use crate::redact::{redact_with_allowlist, REDACTED};
use regex::Regex;
use std::{
    collections::BTreeSet, ffi::OsString, io::Write, path::Path, process::Stdio, sync::OnceLock,
};

pub struct Scrubber {
    env_values: Vec<String>,
    allow_shas: BTreeSet<String>,
}

fn is_hex_sha(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

/// Resolve a revision to its full lowercase 40-hex commit sha in `repo`.
pub fn resolve_commit_sha(repo: &Path, rev: &str) -> Option<String> {
    let rev = rev.trim();
    if rev.is_empty() || !repo.is_dir() {
        return None;
    }
    let out = crate::workspace_resolver::git_command(repo)
        .args([
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{rev}^{{commit}}"),
        ])
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let sha = std::str::from_utf8(&out.stdout)
        .ok()?
        .trim()
        .to_ascii_lowercase();
    is_hex_sha(&sha).then_some(sha)
}

/// Collect lowercase 40-hex strings in `prompt` that resolve as commits in `cwd`.
pub fn collect_prompt_commit_shas(cwd: &Path, prompt: &str) -> BTreeSet<String> {
    static SHA40: OnceLock<Regex> = OnceLock::new();
    let sha_re = SHA40.get_or_init(|| Regex::new(r"\b[0-9a-fA-F]{40}\b").unwrap());
    let candidates: BTreeSet<String> = sha_re
        .find_iter(prompt)
        .map(|m| m.as_str().to_ascii_lowercase())
        .collect();
    if candidates.is_empty() || !cwd.is_dir() {
        return BTreeSet::new();
    }
    let Ok(mut child) = crate::workspace_resolver::git_command(cwd)
        .args(["cat-file", "--batch-check"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    else {
        return BTreeSet::new();
    };
    let stdin = child.stdin.take();
    let out = std::thread::scope(|scope| {
        let candidates = &candidates;
        let writer = scope.spawn(move || {
            let mut stdin = stdin?;
            for sha in candidates {
                writeln!(stdin, "{sha}^{{commit}}").ok()?;
            }
            Some(())
        });
        // Git emits a line per input. Drain its stdout while pumping stdin so
        // neither side can block waiting for the other pipe to empty.
        let output = child.wait_with_output();
        writer.join().ok()??;
        output.ok()
    });
    let Some(out) = out else {
        return BTreeSet::new();
    };
    if !out.status.success() {
        return BTreeSet::new();
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    candidates
        .into_iter()
        .zip(stdout.lines())
        .filter_map(|(sha, line)| (line.split_whitespace().nth(1) == Some("commit")).then_some(sha))
        .collect()
}

impl Scrubber {
    pub fn from_env(env: &[(OsString, OsString)]) -> Self {
        Self::from_env_with_allowlist(env, std::iter::empty::<&str>())
    }
    pub fn from_env_with_allowlist<I, S>(env: &[(OsString, OsString)], allow_shas: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        static NUMBER: OnceLock<Regex> = OnceLock::new();
        let number = NUMBER.get_or_init(|| {
            Regex::new(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$").unwrap()
        });
        let mut values = Vec::new();
        for (name, value) in env {
            let name = name.to_string_lossy().to_ascii_uppercase();
            let value = value.to_string_lossy();
            if [
                "KEY",
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "CREDENTIAL",
                "AUTH",
                "BEARER",
            ]
            .iter()
            .any(|s| name.contains(s))
                && value.chars().count() >= 8
                && !number.is_match(&value)
                && !["true", "false", "yes", "no", "on", "off"]
                    .contains(&value.to_ascii_lowercase().as_str())
            {
                values.push(value.to_string());
                {
                    values.extend(
                        value
                            .split([
                                '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}',
                                '\u{85}', '\u{2028}', '\u{2029}',
                            ])
                            .filter(|s| s.chars().count() >= 8)
                            .map(str::to_owned),
                    );
                }
            }
        }
        // Longest first by character count, as Python's sorted(key=len); ties by bytes.
        values.sort_by_key(|v| (std::cmp::Reverse(v.chars().count()), v.clone()));
        values.dedup();
        let allow_shas = allow_shas
            .into_iter()
            .map(|s| s.as_ref().to_ascii_lowercase())
            .filter(|s| is_hex_sha(s))
            .collect();
        Self {
            env_values: values,
            allow_shas,
        }
    }
    pub fn scrub(&self, text: &str) -> String {
        let mut text = text.to_owned();
        for value in &self.env_values {
            text = text.replace(value, REDACTED);
        }
        redact_with_allowlist(&text, &self.allow_shas)
    }
}
#[cfg(test)]
mod tests {
    use super::Scrubber;
    fn scrubber(env: &[(&str, &str)]) -> Scrubber {
        Scrubber::from_env(
            &env.iter()
                .map(|(k, v)| (k.into(), v.into()))
                .collect::<Vec<_>>(),
        )
    }
    #[test]
    fn python_splitlines_separators_and_char_count_order() {
        for sep in ['\u{1c}', '\u{1d}', '\u{1e}'] {
            let value = format!("aaaa\n{sep}xxxxxxxx");
            let out = scrubber(&[("API_TOKEN", &value)]).scrub("<xxxxxxxx>");
            assert!(!out.contains("xxxxxxxx"), "{sep:?}: {out}");
        }
        // 8 chars (16 bytes) against 12 chars overlapping by one: Python replaces the longer first.
        let (short, long) = ("é".repeat(8), format!("é{}", "b".repeat(11)));
        let s = scrubber(&[("A_TOKEN", &short), ("B_TOKEN", &long)]);
        let out = s.scrub(&format!("{}{}", "é".repeat(7), long));
        assert!(
            !out.contains('b') && out.starts_with(&"é".repeat(7)),
            "{out}"
        );
    }
    #[test]
    fn commit_lookup_drains_large_git_batch() {
        use std::process::Command;
        use std::time::{Duration, Instant};

        const CHILD: &str = "PG_TEST_LARGE_SHA_LOOKUP_CHILD";
        const LOOKUPS: usize = 4000;
        // A deadlock never finishes, so this is only a hang watchdog, far above
        // the slowest honest run (git needs seconds for thousands of misses on Windows).
        const WATCHDOG: Duration = Duration::from_secs(300);
        if std::env::var_os(CHILD).is_some() {
            let repo = tempfile::tempdir().unwrap();
            let git = |args: &[&str]| {
                let out = crate::git_test_support::command(repo.path())
                    .args(args)
                    .output()
                    .unwrap();
                assert!(out.status.success());
                String::from_utf8(out.stdout).unwrap().trim().to_owned()
            };
            git(&["init", "-q"]);
            git(&[
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "init",
            ]);
            let head = git(&["rev-parse", "HEAD"]);
            // About 200 KB each way: past a Linux pipe buffer (64 KiB) and far past
            // a Windows one (4 KiB), so a lookup that does not drain deadlocks.
            let mut prompt: String = (0..LOOKUPS)
                .map(|index| format!("{index:040x}\n"))
                .collect();
            prompt.push_str(&head);
            assert_eq!(
                super::collect_prompt_commit_shas(repo.path(), &prompt),
                std::collections::BTreeSet::from([head])
            );
            return;
        }

        // Isolate the lookup so a blocked pipe cannot hang the test runner.
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "run::scrub::tests::commit_lookup_drains_large_git_batch",
            ])
            .env(CHILD, "1")
            .spawn()
            .unwrap();
        let deadline = Instant::now() + WATCHDOG;
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            if Instant::now() >= deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!(
                    "git batch lookup did not finish within {WATCHDOG:?}: pipes are not drained"
                );
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn allowlist_resolution_and_scrubbing() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| -> String {
            let out = crate::git_test_support::command(dir.path())
                .args(args)
                .output()
                .unwrap();
            assert!(out.status.success());
            String::from_utf8_lossy(&out.stdout).trim().to_owned()
        };
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("f.txt"), "hello\n").unwrap();
        git(&["add", "f.txt"]);
        git(&[
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "-qm",
            "init",
        ]);
        let head = git(&["rev-parse", "HEAD"]);
        let tree = git(&["rev-parse", "HEAD^{tree}"]);
        let missing = "deadbeef".repeat(5);
        assert_eq!(
            super::resolve_commit_sha(dir.path(), "HEAD"),
            Some(head.clone())
        );
        assert_eq!(super::resolve_commit_sha(dir.path(), &tree), None);
        let found = super::collect_prompt_commit_shas(
            dir.path(),
            &format!("commit {head} tree {tree} missing {missing}"),
        );
        assert_eq!(found, std::collections::BTreeSet::from([head.clone()]));
        // Env secrets still win over allowlist, while normal commit output stays readable.
        let s = Scrubber::from_env_with_allowlist(
            &[("MY_TOKEN".into(), missing.clone().into())],
            [&head],
        );
        assert_eq!(
            s.scrub(&format!(
                "HEAD: {head}\nother: {missing}\nlong: 0000{head}0000"
            )),
            format!("HEAD: {head}\nother: [REDACTED]\nlong: [REDACTED]")
        );
    }
}
