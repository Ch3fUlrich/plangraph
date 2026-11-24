use super::build::{build, BuildInput};
use super::build_tests::{g, part, Fx};
use super::deploy::*;
use super::deploy_probe::parse_findings;
use std::collections::{BTreeMap, BTreeSet};

/// One commit on a new branch from main: writes `files`, with `message`; returns the head.
fn commit(fx: &Fx, files: &[(&str, &str)], message: &str) -> String {
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", &fx.base()]);
    for (name, text) in files {
        let path = p.join(name);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    g(p, &["add", "-A"]);
    g(p, &["commit", "-q", "-m", message]);
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    head
}

struct Dry(BTreeMap<(&'static str, &'static str), Vec<Finding>>);

impl Probe for Dry {
    fn probe(&self, host: &str, family: &str) -> Result<(), Vec<Finding>> {
        match self.0.get(&(host, family)) {
            Some(f) => Err(f.clone()),
            None => Ok(()),
        }
    }
}

fn pass() -> Dry {
    Dry(BTreeMap::new())
}

fn hosts(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|s| s.to_string()).collect()
}

/// A unit part (#1, key demo:B, `Deploy: alpha`) and a unitless part (#2, key demo:A).
fn train(fx: &Fx, unit_message: &str) -> Vec<PartDeploy> {
    let unit = commit(
        fx,
        &[("deploy/systemd/train.service", "[Service]\n")],
        unit_message,
    );
    let docs = commit(fx, &[("docs/a.md", "a\n")], "docs\n\nPlan-Item: demo:A");
    let input = BuildInput {
        repo: fx.path(),
        build: "b1".into(),
        base: fx.base(),
        parts: vec![part(1, &unit, None), part(2, &docs, None)],
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    };
    let manifest = build(&input).unwrap().manifest.unwrap();
    derive(fx.path(), &manifest).unwrap()
}

#[test]
fn a_unit_part_and_a_unitless_part_yield_one_attributed_family_and_its_hosts() {
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B\nDeploy: alpha, beta");
    assert_eq!(parts[0].families, ["train.service".to_string()].into());
    assert!(parts[1].families.is_empty());
    let plan = deploy_plan(
        &parts,
        &BTreeMap::new(),
        &hosts(&["alpha", "beta"]),
        &pass(),
    );
    assert!(plan.blockers.is_empty(), "{plan:?}");
    let names: Vec<_> = plan
        .targets
        .iter()
        .map(|t| (t.family.as_str(), t.host.as_str(), t.parts.clone()))
        .collect();
    assert_eq!(
        names,
        [
            ("train.service", "alpha", vec![1]),
            ("train.service", "beta", vec![1])
        ]
    );
    assert_eq!(
        plan.go_req(),
        "deploy: train.service@alpha, train.service@beta"
    );
}

#[test]
fn item_level_deploy_hosts_replace_the_commit_trailers() {
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B\nDeploy: wrong");
    let declared = BTreeMap::from([("demo:B".to_string(), vec!["alpha".to_string()])]);
    let plan = deploy_plan(&parts, &declared, &hosts(&["alpha", "wrong"]), &pass());
    assert_eq!(plan.go_req(), "deploy: train.service@alpha");
}

#[test]
fn a_failed_dry_run_excludes_the_family_and_names_the_blocker_item() {
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B\nDeploy: alpha");
    let dry = Dry(BTreeMap::from([(
        ("alpha", "train.service"),
        vec![Finding {
            kind: "env".into(),
            name: "PG_TOKEN_FILE".into(),
        }],
    )]));
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &dry);
    assert!(plan.targets.is_empty());
    assert_eq!(plan.go_req(), "deploy: none");
    let b = &plan.blockers[0];
    assert_eq!(b.key, "plangraph:LE-DEPLOY-BLOCKED-TRAIN-SERVICE-ALPHA");
    assert_eq!(b.reasons, ["missing env PG_TOKEN_FILE"]);
    let items = plan.blocker_items();
    assert_eq!(items["items"][0]["key"], b.key.as_str());
    assert_eq!(items["items"][0]["under"], "plangraph:LESSONS-ENFORCE");
}

#[test]
fn one_refused_host_keeps_the_passing_host_and_blocks_only_itself() {
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B\nDeploy: alpha, beta");
    let dry = Dry(BTreeMap::from([(
        ("beta", "train.service"),
        vec![Finding {
            kind: "binary".into(),
            name: "pg-x".into(),
        }],
    )]));
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha", "beta"]), &dry);
    assert_eq!(plan.go_req(), "deploy: train.service@alpha");
    assert_eq!(plan.blockers.len(), 1);
    assert_eq!(plan.blockers[0].host.as_deref(), Some("beta"));
}

#[test]
fn an_unauthorized_or_unnamed_host_blocks_without_a_probe() {
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B\nDeploy: stranger");
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &pass());
    assert!(plan.targets.is_empty());
    assert_eq!(
        plan.blockers[0].reasons,
        ["host stranger is not authorized"]
    );
    let fx = Fx::new();
    let parts = train(&fx, "unit\n\nPlan-Item: demo:B");
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &pass());
    assert_eq!(plan.blockers[0].host, None);
    assert_eq!(
        plan.blockers[0].key,
        "plangraph:LE-DEPLOY-BLOCKED-TRAIN-SERVICE-NOHOST"
    );
}

#[test]
fn a_unitless_part_never_blocks_and_binaries_are_families_only_when_added() {
    let fx = Fx::new();
    let a = commit(
        &fx,
        &[("rust/demo/src/main.rs", "fn main() {}\n")],
        "bin\n\nPlan-Item: demo:C\nDeploy: alpha",
    );
    let b = commit(&fx, &[("docs/b.md", "b\n")], "docs\n\nPlan-Item: demo:A");
    let input = BuildInput {
        repo: fx.path(),
        build: "b1".into(),
        base: fx.base(),
        parts: vec![part(1, &a, None), part(2, &b, None)],
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    };
    let manifest = build(&input).unwrap().manifest.unwrap();
    let parts = derive(fx.path(), &manifest).unwrap();
    assert_eq!(parts[0].families, ["bin:demo".to_string()].into());
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &pass());
    assert_eq!(plan.go_req(), "deploy: bin:demo@alpha");
    assert!(plan.blockers.is_empty());
}

#[test]
fn dry_run_output_is_parsed_strictly() {
    let f = parse_findings("MISSING binary pg-x\nMISSING env PG_URL\n").unwrap();
    assert_eq!(f.len(), 2);
    assert_eq!(
        f[1],
        Finding {
            kind: "env".into(),
            name: "PG_URL".into()
        }
    );
    assert!(parse_findings("MISSING socket x\n").is_err());
    assert!(parse_findings("all good\n").is_err());
    let cmd = |argv: &[&str]| CommandProbe {
        argv: argv.iter().map(|s| s.to_string()).collect(),
    };
    assert!(cmd(&["true"]).probe("h", "f").is_ok());
    assert!(cmd(&["false"]).probe("h", "f").is_err());
    assert!(cmd(&[]).probe("h", "f").is_err());
    let missing = cmd(&["sh", "-c", "echo MISSING file /etc/x; exit 1", "sh"]).probe("h", "f");
    assert_eq!(missing.unwrap_err()[0].name, "/etc/x");
}

/// One PR with two commits: the unit commit (item demo:B, host alpha) and, on top of it, a docs-only commit
/// of another item (demo:A, host beta). Only demo:B ships the unit and only alpha is its host.
#[test]
fn a_docs_only_item_of_a_shared_part_inherits_neither_the_family_nor_its_hosts() {
    let fx = Fx::new();
    let p = fx.path();
    let unit = commit(
        &fx,
        &[("deploy/systemd/train.service", "[Service]\n")],
        "unit\n\nPlan-Item: demo:B\nDeploy: alpha",
    );
    g(p, &["checkout", "-q", "--detach", &unit]);
    std::fs::write(p.join("docs.md"), "d\n").unwrap();
    g(p, &["add", "-A"]);
    g(
        p,
        &[
            "commit",
            "-q",
            "-m",
            "docs\n\nPlan-Item: demo:A\nDeploy: beta",
        ],
    );
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    let input = BuildInput {
        repo: p,
        build: "b1".into(),
        base: fx.base(),
        parts: vec![part(1, &head, None)],
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    };
    let manifest = build(&input).unwrap().manifest.unwrap();
    let parts = derive(p, &manifest).unwrap();
    assert_eq!(
        parts[0].items,
        ["demo:A".to_string(), "demo:B".to_string()].into()
    );
    let plan = deploy_plan(
        &parts,
        &BTreeMap::new(),
        &hosts(&["alpha", "beta"]),
        &pass(),
    );
    assert_eq!(plan.go_req(), "deploy: train.service@alpha");
    assert_eq!(plan.targets[0].items, ["demo:B".to_string()].into());
    // Item-level hosts are placed per item too: demo:A declares beta but ships nothing.
    let declared = BTreeMap::from([("demo:A".to_string(), vec!["beta".to_string()])]);
    let plan = deploy_plan(&parts, &declared, &hosts(&["alpha", "beta"]), &pass());
    assert_eq!(plan.go_req(), "deploy: train.service@alpha");
}

/// An item's unit added in an untagged follow-up commit still belongs to the item (the keeper's
/// `part_changes` rule): the item gets the unit and the host it declares.
#[test]
fn a_unit_added_in_an_untagged_follow_up_goes_to_the_items_of_its_part() {
    let fx = Fx::new();
    let p = fx.path();
    let tagged = commit(&fx, &[("docs/a.md", "a\n")], "work\n\nPlan-Item: demo:B");
    g(p, &["checkout", "-q", "--detach", &tagged]);
    std::fs::create_dir_all(p.join("deploy/systemd")).unwrap();
    std::fs::write(p.join("deploy/systemd/late.service"), "[Service]\n").unwrap();
    g(p, &["add", "-A"]);
    g(p, &["commit", "-q", "-m", "follow-up without trailers"]);
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    let input = BuildInput {
        repo: p,
        build: "b1".into(),
        base: fx.base(),
        parts: vec![part(1, &head, None)],
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    };
    let manifest = build(&input).unwrap().manifest.unwrap();
    let parts = derive(p, &manifest).unwrap();
    let declared = BTreeMap::from([("demo:B".to_string(), vec!["alpha".to_string()])]);
    let plan = deploy_plan(&parts, &declared, &hosts(&["alpha"]), &pass());
    assert_eq!(plan.go_req(), "deploy: late.service@alpha");
    assert_eq!(plan.targets[0].items, ["demo:B".to_string()].into());
    assert!(plan.blockers.is_empty(), "{plan:?}");
}
