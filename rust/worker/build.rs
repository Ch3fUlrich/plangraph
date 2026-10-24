//! Stamps the worker binary with the git revision it was built from.
//!
//! Temporal versioning pins a running workflow to the build that started it, so
//! every worker binary needs a stable, reproducible build id. Baking the short
//! SHA in at compile time keeps it stable across restarts of the same binary.
//! The script never fails the build: a missing git or checkout just yields
//! `nogit`.

use std::process::Command;

fn main() {
    // Rebuild only when this script or the explicit build-id override changes.
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-env-changed=PG_BUILD_ID");

    let sha = git_short_head().unwrap_or_else(|| "nogit".to_string());
    println!("cargo:rustc-env=PG_BUILD_SHA={sha}");
}

/// `git rev-parse --short=12 HEAD`, or `None` on any failure (no git, not a
/// repository, empty output) so the caller can fall back rather than panic.
fn git_short_head() -> Option<String> {
    let out = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let sha = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (!sha.is_empty()).then_some(sha)
}
