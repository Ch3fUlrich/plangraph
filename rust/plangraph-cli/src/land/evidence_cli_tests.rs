//! The carries file `evidence-assess --carry-out` writes goes through the `build` and `verify` commands;
//! a refusal by carry admission is told from a failure of the freeze by its message.
use super::build::BuildResult;
use super::build_tests::{g, part, Fx};
use super::evidence::Evidence;
use super::evidence_git;
use super::main_state::State;
use super::train::*;
use serde::Serialize;
use std::collections::BTreeMap;
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

/// A batch whose head was amended on the same base: same patch, so its ACCEPT and CI carry.
struct Case {
    fx: Fx,
    dir: tempfile::TempDir,
    base: String,
    old: String,
    new: String,
    carries: String,
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
    // The file the CLI writes: `evidence-assess --carry-out`.
    let dir = tempfile::tempdir().unwrap();
    let carries = dir.path().join("carries.json").to_str().unwrap().to_owned();
    let flags: BTreeMap<String, String> = [
        ("repo", fx.path().to_str().unwrap()),
        ("evidence", &file(dir.path(), "evidence.json", &evidence)),
        ("main", &base),
        ("head", &new),
        ("trains-now", "1"),
        ("carry-out", &carries),
    ]
    .iter()
    .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
    .collect();
    super::evidence_cli::assess_cli(&flags).unwrap();
    Case {
        fx,
        dir,
        base,
        old,
        new,
        carries,
    }
}

impl Case {
    /// The parts file of the registrar: the part's receipt is the one of the old head.
    fn parts(&self) -> Vec<PartIn> {
        let mut p = part(1, &self.new, None);
        p.receipt.head = self.old.clone();
        vec![p]
    }

    fn flags(&self, carries: bool, trains: &str, out: &Path) -> BTreeMap<String, String> {
        let mut f: BTreeMap<String, String> = [
            ("repo", self.fx.path().to_str().unwrap()),
            ("base", &self.base),
            ("name", "t1"),
            ("grant-digest", "fixture"),
            ("snapshot-position", "1"),
            ("out", out.to_str().unwrap()),
            ("trains-landed", trains),
        ]
        .iter()
        .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
        .collect();
        if carries {
            f.insert("carries".into(), self.carries.clone());
        }
        f
    }

    fn build(
        &self,
        parts: Vec<PartIn>,
        carries: bool,
        trains: &str,
        out: &Path,
    ) -> Result<Vec<String>, String> {
        let flags = self.flags(carries, trains, out);
        super::cli::run_build(&flags, parts, vec![], &State::Green, &self.base)
    }

    /// `verify` over the artifacts `build` wrote, with the parts as the registrar gives them.
    fn verify(&self, out: &Path, carries: bool, trains: &str) -> Result<Vec<String>, String> {
        let read = |n: &str| -> serde_json::Value {
            serde_json::from_slice(&std::fs::read(out.join(n)).unwrap()).unwrap()
        };
        let open: BuildOpen = serde_json::from_value(read("t1.open.json")).unwrap();
        let result: BuildResult = serde_json::from_value(read("t1.result.json")).unwrap();
        let freeze = Freeze {
            policy_version: open.policy_version.clone(),
            base: open.base.clone(),
            grant_digest: open.grant_digest.clone(),
            selection: result.snapshot.clone(),
        };
        let d = self.dir.path();
        let mut args: Vec<String> = [
            "verify",
            "--repo",
            self.fx.path().to_str().unwrap(),
            "--manifest",
            out.join("t1.manifest.json").to_str().unwrap(),
            "--parts",
            &file(d, "parts.json", &self.parts()),
            "--open",
            out.join("t1.open.json").to_str().unwrap(),
            "--freeze",
            &file(d, "freeze.json", &freeze),
            "--result",
            out.join("t1.result.json").to_str().unwrap(),
        ]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
        if carries {
            args.extend([
                "--carries".into(),
                self.carries.clone(),
                "--trains-landed".into(),
                trains.into(),
            ]);
        }
        super::cli::run_inner(d, &args, &python)
    }
}

type Out = tempfile::TempDir;

#[test]
fn the_carries_file_of_evidence_assess_is_admitted_by_build_and_verify_within_the_bound() {
    let c = case();
    let out: Out = tempfile::tempdir().unwrap();
    c.build(c.parts(), true, "1", out.path()).unwrap();
    c.verify(out.path(), true, "1").unwrap();
}

#[test]
fn build_and_verify_refuse_the_receipt_of_the_old_head_without_a_carry_it_can_admit() {
    let c = case();
    let good: Out = tempfile::tempdir().unwrap();
    c.build(c.parts(), true, "1", good.path()).unwrap();
    // Without --carries, and past the train bound: the build refuses at admission, by the receipt.
    for (carries, trains) in [(false, "1"), (true, "4")] {
        let out: Out = tempfile::tempdir().unwrap();
        let err = c.build(c.parts(), carries, trains, out.path()).unwrap_err();
        assert!(err.contains("receipt:"), "{err}");
    }
    // The verifier refuses the same artifacts the same way, after the freeze matched: not a freeze failure.
    for (carries, trains) in [(false, "1"), (true, "4")] {
        let err = c.verify(good.path(), carries, trains).unwrap_err();
        assert!(err.contains("receipt:"), "{err}");
        assert!(!err.contains("freeze refused"), "{err}");
    }
}

#[test]
fn a_carry_embedded_in_the_parts_is_dropped_by_the_build_too() {
    let c = case();
    let carry: Vec<Carry> = serde_json::from_slice(&std::fs::read(&c.carries).unwrap()).unwrap();
    let mut parts = c.parts();
    parts[0].receipt.carried = Some(carry[0].clone());
    let out: Out = tempfile::tempdir().unwrap();
    let err = c.build(parts, false, "1", out.path()).unwrap_err();
    assert!(err.contains("receipt:"), "{err}");
}
