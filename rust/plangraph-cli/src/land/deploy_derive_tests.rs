use super::build::{build, BuildInput};
use super::build_tests::{g, part, Fx};
use super::deploy::*;
use super::deploy_tests::{commit, hosts, pass};
use std::collections::BTreeMap;

fn input<'a>(fx: &'a Fx, parts: Vec<super::train::PartIn>) -> BuildInput<'a> {
    BuildInput {
        repo: fx.path(),
        build: "b1".into(),
        base: fx.base(),
        parts,
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    }
}

/// An untagged commit that edits a binary an earlier commit of the part added belongs to the part's items
/// like any untagged follow-up: the keeper requires `bin:demo` for demo:A too.
#[test]
fn an_untagged_edit_of_a_newly_added_binary_goes_to_the_items_of_its_part() {
    let fx = Fx::new();
    let p = fx.path();
    let added = commit(
        &fx,
        &[("rust/demo/src/main.rs", "fn main() {}\n")],
        "bin\n\nPlan-Item: demo:B\nDeploy: alpha",
    );
    g(p, &["checkout", "-q", "--detach", &added]);
    std::fs::write(p.join("docs.md"), "d\n").unwrap();
    g(p, &["add", "-A"]);
    g(p, &["commit", "-q", "-m", "docs\n\nPlan-Item: demo:A"]);
    std::fs::write(p.join("rust/demo/src/main.rs"), "fn main() { 1; }\n").unwrap();
    g(p, &["add", "-A"]);
    g(p, &["commit", "-q", "-m", "tweak without trailers"]);
    let head = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    let manifest = build(&input(&fx, vec![part(1, &head, None)]))
        .unwrap()
        .manifest
        .unwrap();
    let parts = derive(p, &manifest).unwrap();
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &pass());
    assert_eq!(plan.go_req(), "deploy: bin:demo@alpha");
    assert_eq!(
        plan.targets[0].items,
        ["demo:A".to_string(), "demo:B".to_string()].into()
    );
}

/// A stacked train whose later part removes the unit an earlier part added ships no unit: the plan is
/// generated from the net train, so no target is probed or named for it, and a surviving unit stays.
#[test]
fn a_unit_a_later_part_removes_is_no_target_and_a_surviving_unit_stays() {
    let fx = Fx::new();
    let p = fx.path();
    let one = commit(
        &fx,
        &[
            ("deploy/systemd/gone.service", "[Service]\n"),
            ("deploy/systemd/kept.service", "[Service]\n"),
        ],
        "units\n\nPlan-Item: demo:B\nDeploy: alpha",
    );
    g(p, &["checkout", "-q", "--detach", &one]);
    g(p, &["rm", "-q", "deploy/systemd/gone.service"]);
    g(p, &["commit", "-q", "-m", "drop gone\n\nPlan-Item: demo:C"]);
    let two = g(p, &["rev-parse", "HEAD"]);
    g(p, &["checkout", "-q", "main"]);
    let manifest = build(&input(
        &fx,
        vec![part(1, &one, None), part(2, &two, Some((1, &one)))],
    ))
    .unwrap()
    .manifest
    .unwrap();
    let parts = derive(p, &manifest).unwrap();
    let plan = deploy_plan(&parts, &BTreeMap::new(), &hosts(&["alpha"]), &pass());
    assert_eq!(plan.go_req(), "deploy: kept.service@alpha");
    assert!(plan.blockers.is_empty(), "{plan:?}");
}
