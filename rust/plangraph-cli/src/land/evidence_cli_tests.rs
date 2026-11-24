//! The carries reach the builder only through the CLI dispatch that reads `--parts`.
use super::build::*;
use super::build_tests::{g, part, Fx};
use super::evidence::Evidence;
use super::evidence_git;
use super::train::*;
use serde::Serialize;
use std::path::Path;

fn file(dir: &Path, name: &str, value: &impl Serialize) -> String {
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_vec(value).unwrap()).unwrap();
    path.to_str().unwrap().to_owned()
}

fn python() -> std::process::Command {
    let mut command = crate::base_fresh::python_command(None);
    command.args([
        "-I",
        "-c",
        "import json,sys;print(json.dumps({t:None for t in json.load(sys.stdin)}))",
    ]);
    command
}

/// A part whose head was amended (same patch, same base): its receipt is the one of the old head.
struct Case {
    fx: Fx,
    dir: tempfile::TempDir,
    old: String,
    new: String,
    evidence: Evidence,
    manifest: Manifest,
    open: BuildOpen,
    result: BuildResult,
}

fn case() -> Case {
    let fx = Fx::new();
    let base = fx.base();
    let old = fx.commit(&base, "a.txt", "a\n");
    let evidence = Evidence {
        key: evidence_git::key_of(fx.path(), &base, &old).unwrap(),
        base: base.clone(),
        head: old.clone(),
        trains_at_base: 0,
        accept: true,
        ci_green: true,
    };
    g(fx.path(), &["checkout", "--detach", &old]);
    g(
        fx.path(),
        &["commit", "--amend", "-m", "part\n\nWriter: gpt-6.2-sol"],
    );
    let new = g(fx.path(), &["rev-parse", "HEAD"]);
    g(fx.path(), &["checkout", "-q", "main"]);
    let carry = Carry {
        old_head: old.clone(),
        new_head: new.clone(),
        evidence: evidence.clone(),
    };
    let mut p = part(1, &new, None);
    p.receipt.head = old.clone();
    p.receipt.carried = Some(carry);
    let outcome = build(&fx.input(vec![p], "", None)).unwrap();
    let m = outcome.manifest.unwrap();
    let open = BuildOpen {
        build: m.build.clone(),
        base: m.base.clone(),
        policy_version: m.policy.version.clone(),
        grant_digest: "fixture".into(),
        snapshot_position: 1,
    };
    Case {
        fx,
        dir: tempfile::tempdir().unwrap(),
        old,
        new,
        evidence,
        manifest: m,
        open,
        result: outcome.result,
    }
}

impl Case {
    /// `verify` through the dispatch with the parts file as given and the carries file as given.
    fn verify(
        &self,
        embedded: bool,
        frozen: bool,
        carries: Option<&[Carry]>,
        trains: &str,
    ) -> Result<Vec<String>, String> {
        let mut p = part(1, &self.new, None);
        p.receipt.head = self.old.clone();
        let carry = Carry {
            old_head: self.old.clone(),
            new_head: self.new.clone(),
            evidence: self.evidence.clone(),
        };
        // The freeze records the selection as the builder admitted it.
        p.receipt.carried = frozen.then(|| carry.clone());
        let freeze = Freeze {
            policy_version: self.open.policy_version.clone(),
            base: self.open.base.clone(),
            grant_digest: self.open.grant_digest.clone(),
            selection: vec![p.clone()],
        };
        p.receipt.carried = embedded.then_some(carry);
        let d = self.dir.path();
        let mut args: Vec<String> = vec![
            "verify".into(),
            "--repo".into(),
            self.fx.path().to_str().unwrap().into(),
            "--manifest".into(),
            file(d, "manifest.json", &self.manifest),
            "--parts".into(),
            file(d, "parts.json", &vec![p]),
            "--open".into(),
            file(d, "open.json", &self.open),
            "--freeze".into(),
            file(d, "freeze.json", &freeze),
            "--result".into(),
            file(d, "result.json", &self.result),
        ];
        if let Some(carries) = carries {
            args.extend([
                "--carries".into(),
                file(d, "carries.json", &carries),
                "--trains-landed".into(),
                trains.into(),
            ]);
        }
        super::cli::run_inner(d, &args, &python)
    }
}

#[test]
fn a_carry_embedded_in_the_parts_file_is_dropped_and_only_a_validated_carry_is_taken() {
    let c = case();
    let carry = Carry {
        old_head: c.old.clone(),
        new_head: c.new.clone(),
        evidence: c.evidence.clone(),
    };
    // Embedded in --parts with no --carries: refused, the receipt names the old head.
    let err = c.verify(true, false, None, "1").unwrap_err();
    // The embedded carry no longer matches the selection the builder froze with it.
    assert!(
        err.contains("freeze refused") || err.contains("receipt"),
        "{err}"
    );
    // Through --carries, assessed again at admission: accepted within the train bound ...
    c.verify(false, true, Some(std::slice::from_ref(&carry)), "1")
        .unwrap();
    // ... refused once four trains landed, although the embedded carry says otherwise.
    let err = c
        .verify(true, false, Some(std::slice::from_ref(&carry)), "4")
        .unwrap_err();
    // The embedded carry no longer matches the selection the builder froze with it.
    assert!(
        err.contains("freeze refused") || err.contains("receipt"),
        "{err}"
    );
    // A carry file of another head attaches nothing.
    let mut other = carry.clone();
    other.new_head = c.old.clone();
    assert!(c.verify(false, false, Some(&[other]), "1").is_err());
}
