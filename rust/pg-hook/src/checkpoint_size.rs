//! Package-time checkpoint budget, before any review seats are launched.
use std::process::{Command, ExitCode};

// Interim PHASE-BATCH-SIZE-EXPERIMENT defaults; keep the phase-batch skill in sync.
const MAX_BYTES: usize = 100_000;
const MAX_FILES: usize = 25;

fn git(args: &[&str]) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let output = Command::new("git").args(args).output()?;
    if !output.status.success() {
        return Err(format!("git failed: {}", String::from_utf8_lossy(&output.stderr)).into());
    }
    Ok(output.stdout)
}

fn revision(reference: &str) -> Result<String, Box<dyn std::error::Error>> {
    let bytes = git(&[
        "rev-parse",
        "--verify",
        "--end-of-options",
        &format!("{reference}^{{commit}}"),
    ])?;
    Ok(String::from_utf8(bytes)?.trim().to_owned())
}

fn over_budget(bytes: usize, files: usize) -> bool {
    bytes >= MAX_BYTES || files > MAX_FILES
}

fn run() -> Result<bool, Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 2 {
        return Err("usage: checkpoint-size BASE HEAD (committed PR range)".into());
    }
    let base = revision(&args[0])?;
    let head = revision(&args[1])?;
    let range = format!("{base}...{head}");
    // Include binary payloads and disable user diff helpers. NUL delimiters count
    // arbitrary path names correctly; no-renames makes path counting conservative.
    let diff = git(&[
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--no-relative",
        "--unified=3",
        "--ignore-submodules=none",
        "--submodule=short",
        "--binary",
        "--full-index",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        &range,
        "--",
    ])?;
    let paths = git(&[
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-relative",
        "--ignore-submodules=none",
        "--name-only",
        "-z",
        &range,
        "--",
    ])?;
    let files = paths.iter().filter(|&&byte| byte == 0).count();
    let oversized = over_budget(diff.len(), files);
    println!(
        "checkpoint-size: {} bytes, {files} files; budget <{MAX_BYTES} bytes and <={MAX_FILES} files ({base}...{head})",
        diff.len()
    );
    if oversized {
        println!(
            "checkpoint-size: refused; split into stacked checkpoints at package time before seats"
        );
    }
    Ok(oversized)
}

fn main() -> ExitCode {
    match run() {
        Ok(false) => ExitCode::SUCCESS,
        Ok(true) => ExitCode::from(1),
        Err(error) => {
            eprintln!("checkpoint-size: {error}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn checkpoint_budget_boundaries() {
        assert!(!over_budget(99_999, 25));
        assert!(over_budget(100_000, 1));
        assert!(over_budget(1, 26));
        assert!(!over_budget(0, 0));
    }
}
