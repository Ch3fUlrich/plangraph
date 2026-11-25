//! Classifies a red CI job from its name and scrubbed log, and decides the first action for the class.
//! The class changes what a fix writer is told, never whether one is dispatched (rule 3.46).
use std::collections::BTreeSet;

/// Why a job is red. `Mixed` is a property of a run whose red jobs differ; no single job has it.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum RedClass {
    Fmt,
    Clippy,
    FileSize,
    MissingTrailer,
    MissingModule,
    Flake,
    Code,
    Infrastructure,
    Configuration,
    Unknown,
    Mixed,
}

/// First action for a red job. A task and a brief are made in every case but a flake that still has its rerun.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Action {
    /// Rerun the failed jobs once; the flake item takes the second sighting.
    RerunOnce,
    /// A machine-applicable fix, committed by the watcher with the given `Hand-Edit` trailer line.
    AutoFix {
        command: &'static [&'static str],
        /// Directory, below the repository root, the command runs in.
        dir: &'static str,
        trailer: String,
    },
    /// A narrow fix writer started from the class, the job and its log lines.
    FixWriter,
}

/// Clippy lints whose suggestion `cargo clippy --fix` applies without a decision.
const MACHINE_APPLICABLE: &[&str] = &[
    "needless_borrow",
    "needless_borrows_for_generic_args",
    "redundant_clone",
    "redundant_closure",
    "needless_return",
    "redundant_field_names",
    "single_char_pattern",
    "useless_format",
    "clone_on_copy",
    "len_zero",
    "manual_map",
    "collapsible_if",
    "collapsible_else_if",
    "uninlined_format_args",
    "needless_lifetimes",
    "unnecessary_to_owned",
    "explicit_auto_deref",
];

const INFRASTRUCTURE: &[&str] = &[
    "runner has received a shutdown signal",
    "no space left on device",
    "lost communication with the server",
    "could not resolve host",
    "failed to download",
    "connection reset by peer",
    "error: rate limit",
    "api rate limit exceeded",
    "http 502",
    "http 503",
    "http 504",
    "the operation was canceled",
    "unable to access 'https://",
];

const CONFIGURATION: &[&str] = &[
    "invalid workflow file",
    "unrecognized named-value",
    "resource not accessible by integration",
    "command not found",
    "exit code 127",
    "secret is not set",
    "yaml syntax error",
];

impl RedClass {
    pub fn name(self) -> &'static str {
        match self {
            Self::Fmt => "fmt",
            Self::Clippy => "clippy",
            Self::FileSize => "file-size",
            Self::MissingTrailer => "missing-trailer",
            Self::MissingModule => "missing-module",
            Self::Flake => "flake",
            Self::Code => "code",
            Self::Infrastructure => "infrastructure",
            Self::Configuration => "configuration",
            Self::Unknown => "unknown",
            Self::Mixed => "mixed",
        }
    }

    pub fn from_name(name: &str) -> Option<Self> {
        [
            Self::Fmt,
            Self::Clippy,
            Self::FileSize,
            Self::MissingTrailer,
            Self::MissingModule,
            Self::Flake,
            Self::Code,
            Self::Infrastructure,
            Self::Configuration,
            Self::Unknown,
            Self::Mixed,
        ]
        .into_iter()
        .find(|class| class.name() == name)
    }

    /// Where the fix writer starts looking.
    pub fn start_at(self) -> &'static str {
        match self {
            Self::Infrastructure => "the runner, the network or the action versions of the job; not the batch's code",
            Self::Configuration => "the workflow file, the runner image or a repository setting; not the batch's code",
            Self::Fmt => "formatting of the changed files (`cargo fmt`)",
            Self::Clippy => "the lint named in the log (`cargo clippy --fix` when machine-applicable)",
            Self::FileSize => "the file named in the log: split it, never raise a limit",
            Self::MissingTrailer => "the commit message trailers of the head (the gate names the trailer)",
            Self::MissingModule => "the job's installed tools: a test or tool imports a module the job does not install",
            Self::Flake => "the FLAKE item of the test; the job was rerun once",
            Self::Code => "the failing test named in the log",
            Self::Unknown | Self::Mixed => "the log lines below; the class could not be settled from one pattern",
        }
    }
}

fn lints(log: &str) -> BTreeSet<String> {
    let mut found = BTreeSet::new();
    for line in log.lines() {
        let mut rest = line;
        while let Some(at) = rest.find("clippy::") {
            let tail = &rest[at + "clippy::".len()..];
            let end = tail
                .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-'))
                .unwrap_or(tail.len());
            if end > 0 {
                found.insert(tail[..end].replace('-', "_"));
            }
            rest = &tail[end..];
        }
    }
    found
}

/// True when the log names a failing test: a pytest `FAILED id` line, a Rust `... FAILED` line or a panic.
fn names_failing_tests(log: &str) -> bool {
    log.lines().any(|line| {
        let line = line.trim_start_matches(|c: char| !c.is_ascii_alphabetic());
        line.starts_with("FAILED ") || line.contains(" FAILED ") || line.contains("... FAILED")
    }) || {
        let lower = log.to_ascii_lowercase();
        lower.contains("panicked at") || lower.contains("test result: failed")
    }
}

/// Class of one red job. `known_flake` is true when a failing test of the job maps to a FLAKE item.
pub fn classify_job(job: &str, log: &str, known_flake: bool) -> RedClass {
    if known_flake {
        return RedClass::Flake;
    }
    let lower = log.to_ascii_lowercase();
    let name = job.to_ascii_lowercase();
    let has = |needles: &[&str]| needles.iter().any(|n| lower.contains(n));
    // A log that names failing tests and also an infrastructure or configuration phrase is unsettled: mixed.
    let names_tests = names_failing_tests(log);
    if has(INFRASTRUCTURE) || has(CONFIGURATION) {
        return if names_tests {
            RedClass::Mixed
        } else if has(INFRASTRUCTURE) {
            RedClass::Infrastructure
        } else {
            RedClass::Configuration
        };
    }
    let clippy = lints(log);
    if !clippy.is_empty() && lower.contains("could not compile") {
        let all = clippy
            .iter()
            .all(|l| MACHINE_APPLICABLE.contains(&l.as_str()));
        return if all {
            RedClass::Clippy
        } else {
            RedClass::Code
        };
    }
    if (lower.contains("diff in ") && lower.contains(".rs")) || lower.contains("would reformat") {
        return RedClass::Fmt;
    }
    if name.contains("file size") || lower.contains("check-file-size:") {
        return RedClass::FileSize;
    }
    if lower.contains("missing writer trailer")
        || lower.contains("malformed writer trailer")
        || lower.contains("missing plan-item")
    {
        return RedClass::MissingTrailer;
    }
    if lower.contains("modulenotfounderror") || lower.contains("no module named") {
        // A Python job without its tool is a setup problem; any other job importing one is a code defect.
        let python_job = name.contains("python") || name.contains("pytest");
        return if python_job {
            RedClass::Configuration
        } else {
            RedClass::MissingModule
        };
    }
    if has(&[
        "panicked at",
        "assertionerror",
        "error[e",
        "test result: failed",
        "failed tests/",
        " failed [",
    ]) || log.contains(" FAILED ")
        || log.contains("... FAILED")
    {
        return RedClass::Code;
    }
    RedClass::Unknown
}

/// Class of a run: the class of its red jobs when they agree, otherwise `Mixed`.
pub fn classify_run(classes: &[RedClass]) -> RedClass {
    let distinct: BTreeSet<_> = classes.iter().copied().collect();
    match distinct.len() {
        0 => RedClass::Unknown,
        1 => classes[0],
        _ => RedClass::Mixed,
    }
}

/// The lines a fix task and a failure record carry so a writer starts where the class says: the job class, the
/// run class, where to look first and the failed job id. Empty for a row that has no class.
pub fn brief_lines(class: &str, run_class: &str, job_id: u64) -> String {
    if class.is_empty() {
        return String::new();
    }
    let start = RedClass::from_name(class).map_or("the log lines below", RedClass::start_at);
    format!("Class: {class} (run: {run_class})\nStart at: {start}\nFailed job id: {job_id}\n")
}

/// Line a commit made for this job carries: the gate's `Hand-Edit` form naming the class and the job id.
pub fn hand_edit_trailer(class: RedClass, job_id: u64) -> String {
    format!(
        "Hand-Edit: ci-watch {} fix of failed job {job_id}",
        class.name()
    )
}

/// The Cargo workspace a Rust job checks, below the repository root: the root workspace excludes the standalone
/// worker crate (rust/worker has a workspace of its own), so each needs its own cargo run.
pub fn workspace_dir(job: &str) -> Option<&'static str> {
    let job = job.to_ascii_lowercase();
    if job.contains("worker crate") {
        Some("rust/worker")
    } else if job.contains("workspace on linux") {
        Some("rust")
    } else {
        None
    }
}

/// First action for `class`. `fixed` holds the (head, class) pairs the watcher already fixed, so one head gets
/// one automatic fix per class and a second red of that class goes to a writer. A flake is rerun once.
pub fn decide(
    class: RedClass,
    head: &str,
    job: &str,
    job_id: u64,
    attempted: bool,
    fixed: &BTreeSet<(String, RedClass)>,
) -> Action {
    match class {
        RedClass::Flake if !attempted => Action::RerunOnce,
        RedClass::Fmt | RedClass::Clippy if !fixed.contains(&(head.to_owned(), class)) => {
            // Cargo is run in the workspace the failed job checked; a job of no known workspace gets a writer.
            let Some(dir) = workspace_dir(job) else {
                return Action::FixWriter;
            };
            Action::AutoFix {
                command: if class == RedClass::Fmt {
                    &["cargo", "fmt", "--all"]
                } else {
                    &["cargo", "clippy", "--fix", "--allow-dirty", "--all-targets"]
                },
                dir,
                trailer: hand_edit_trailer(class, job_id),
            }
        }
        _ => Action::FixWriter,
    }
}

#[cfg(test)]
#[path = "classify_tests.rs"]
mod tests;
