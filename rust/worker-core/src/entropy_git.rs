//! Git collection is read-only; tests use disposable repositories exclusively.
#[cfg(test)]
#[path = "entropy_git_tests.rs"]
mod tests;
use crate::entropy::{Baseline, Change, Input, Inventory};
use std::{
    collections::HashSet,
    ffi::OsString,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Command, Stdio},
};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
pub struct Git {
    pub root: PathBuf,
    pub binary: OsString,
}
impl Git {
    fn command(&self) -> Command {
        let mut cmd = Command::new(&self.binary);
        cmd.current_dir(&self.root);
        for (key, _) in std::env::vars_os() {
            let k = key.to_string_lossy().to_ascii_uppercase();
            if ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"].contains(&k.as_str())
                || k.starts_with("GIT_CONFIG")
            {
                cmd.env_remove(key);
            }
        }
        cmd
    }
    fn run(&self, args: &[&str]) -> Result<Vec<u8>> {
        let out = self.command().args(args).output()?;
        if !out.status.success() {
            return Err("git evidence unavailable".into());
        }
        Ok(out.stdout)
    }
    pub fn revision(&self, revision: &str) -> Result<String> {
        let sha = String::from_utf8(self.run(&[
            "rev-parse",
            "--verify",
            "--end-of-options",
            &format!("{revision}^{{commit}}"),
        ])?)?
        .trim()
        .to_string();
        if sha.len() != 40 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("invalid commit".into());
        }
        Ok(sha)
    }
    pub fn inventory(&self, revision: &str) -> Result<Inventory> {
        let tree = self.run(&["ls-tree", "-r", "-z", "--full-tree", revision])?;
        let mut entries = Vec::new();
        for row in tree.split(|b| *b == 0).filter(|r| !r.is_empty()) {
            let (meta, path) = std::str::from_utf8(row)?
                .split_once('\t')
                .ok_or("invalid tree row")?;
            let fields: Vec<_> = meta.split_whitespace().collect();
            if fields.len() != 3 {
                return Err("invalid tree row".into());
            }
            if fields[1] == "blob" && fields[0] != "120000" {
                entries.push((fields[2].to_string(), path.to_string()));
            }
        }
        let mut child = self
            .command()
            .args(["cat-file", "--batch"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let mut input = child.stdin.take().ok_or("missing stdin")?;
        let requests = entries
            .iter()
            .map(|(sha, _)| format!("{sha}\n"))
            .collect::<String>();
        let writer = std::thread::spawn(move || -> std::io::Result<()> {
            input.write_all(requests.as_bytes())
        });
        let mut out = BufReader::new(child.stdout.take().ok_or("missing stdout")?);
        let mut inventory = Inventory::default();
        let read = (|| -> Result<()> {
            for (expected, path) in entries {
                let mut header = String::new();
                out.read_line(&mut header)?;
                let fields: Vec<_> = header.split_whitespace().collect();
                if fields.len() != 3 || fields[0] != expected || fields[1] != "blob" {
                    return Err("invalid blob header".into());
                }
                let size: usize = fields[2].parse()?;
                let mut bytes = vec![0; size];
                out.read_exact(&mut bytes)?;
                let mut newline = [0];
                out.read_exact(&mut newline)?;
                if newline != *b"\n" {
                    return Err("invalid blob terminator".into());
                }
                inventory.add(&path, &bytes);
            }
            Ok(())
        })();
        if read.is_err() {
            let _ = child.kill();
        }
        drop(out);
        let status = child.wait()?;
        writer.join().map_err(|_| "git writer failed")??;
        read?;
        if !status.success() {
            return Err("git batch failed".into());
        }
        Ok(inventory)
    }
    pub fn baseline(&self, revision: &str) -> Result<Option<Baseline>> {
        let path = format!("{revision}:.entropy-baseline.json");
        // Missing is distinct from an unreadable or malformed baseline.
        let names = self.run(&[
            "ls-tree",
            "--name-only",
            revision,
            "--",
            ".entropy-baseline.json",
        ])?;
        if names.is_empty() {
            return Ok(None);
        }
        Ok(Some(serde_json::from_slice(&self.run(&["show", &path])?)?))
    }
    pub fn input(&self, base: &str, head: &str, body: String) -> Result<Input> {
        let head = self.revision(head)?;
        let base = self.revision(base)?;
        let ancestor = String::from_utf8(self.run(&["merge-base", &base, &head])?)?
            .trim()
            .to_string();
        let range = format!("{ancestor}..{head}");
        let new = self.run(&[
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=A",
            "-z",
            &range,
        ])?;
        let new: HashSet<_> = new
            .split(|b| *b == 0)
            .filter(|s| !s.is_empty())
            .map(|s| std::str::from_utf8(s).map(str::to_string))
            .collect::<std::result::Result<_, _>>()?;
        let stats = self.run(&["diff", "--numstat", "--no-renames", "-z", &range])?;
        let mut changes = Vec::new();
        for row in stats.split(|b| *b == 0).filter(|r| !r.is_empty()) {
            let parts: Vec<_> = std::str::from_utf8(row)?.splitn(3, '\t').collect();
            if parts.len() != 3 {
                return Err("invalid numstat".into());
            }
            let number = |s: &str| -> Result<u64> {
                if s == "-" {
                    Ok(0)
                } else {
                    Ok(s.parse()?)
                }
            };
            changes.push(Change {
                path: parts[2].into(),
                added: number(parts[0])?,
                deleted: number(parts[1])?,
                new: new.contains(parts[2]),
            });
        }
        let before = (chrono::Utc::now() - chrono::Duration::days(7)).to_rfc3339();
        let week = String::from_utf8(self.run(&[
            "rev-list",
            "-1",
            &format!("--before={before}"),
            &ancestor,
        ])?)?
        .trim()
        .to_string();
        if week.is_empty() {
            return Err("seven-day history unavailable".into());
        }
        let baseline = self.baseline(&head)?.ok_or("baseline missing from head")?;
        Ok(Input {
            changes,
            current: self.inventory(&head)?,
            week: self.inventory(&week)?,
            baseline,
            prior_baseline: self.baseline(&ancestor)?,
            body,
        })
    }
}
