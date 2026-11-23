//! The host install dry run of one deploy target: its trait, its findings and the command that runs it.
use serde::{Deserialize, Serialize};
use std::process::Command;

/// One finding of the host install dry run.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Finding {
    /// `binary`, `file`, `env` or `path`.
    pub kind: String,
    pub name: String,
}

/// The host install dry run for one target: `Ok` when binaries, files, env keys and paths are all there.
pub trait Probe {
    fn probe(&self, host: &str, family: &str) -> Result<(), Vec<Finding>>;
}

/// Runs `argv... HOST FAMILY`. Exit 0 with no output is a pass; the command prints one
/// `MISSING <binary|file|env|path> <name>` line per absent thing. Anything else refuses the target.
pub struct CommandProbe {
    pub argv: Vec<String>,
}

pub fn parse_findings(text: &str) -> Result<Vec<Finding>, String> {
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            let mut w = l.split_whitespace();
            match (w.next(), w.next(), w.next(), w.next()) {
                (
                    Some("MISSING"),
                    Some(k @ ("binary" | "file" | "env" | "path")),
                    Some(n),
                    None,
                ) => Ok(Finding {
                    kind: k.into(),
                    name: n.into(),
                }),
                _ => Err(format!("unreadable dry-run line {l:?}")),
            }
        })
        .collect()
}

impl Probe for CommandProbe {
    fn probe(&self, host: &str, family: &str) -> Result<(), Vec<Finding>> {
        let refuse = |why: String| {
            Err(vec![Finding {
                kind: "dry-run".into(),
                name: why,
            }])
        };
        let Some((cmd, args)) = self.argv.split_first() else {
            return refuse("no dry-run command configured".into());
        };
        let out = match Command::new(cmd).args(args).arg(host).arg(family).output() {
            Ok(out) => out,
            Err(e) => return refuse(format!("dry run did not start: {e}")),
        };
        let stdout = String::from_utf8_lossy(&out.stdout);
        match (out.status.success(), parse_findings(&stdout)) {
            (true, Ok(f)) if f.is_empty() => Ok(()),
            (_, Ok(f)) if !f.is_empty() => Err(f),
            (_, Err(e)) => refuse(e),
            _ => refuse(format!("dry run exited {}", out.status)),
        }
    }
}
