use super::build::{build, BuildInput};
use super::build_tests::{g, part, Fx};
use super::deploy::*;
use super::deploy_derive::{Crates, Index};
use crate::deploy_manifest::Manifest;
use std::collections::{BTreeMap, BTreeSet};

/// One commit on `from` writing `files` with `message`; returns it and leaves `main` checked out.
fn commit_on(fx: &Fx, from: &str, files: &[(&str, &str)], message: &str) -> String {
    let p = fx.path();
    g(p, &["checkout", "-q", "--detach", from]);
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

fn set(v: &[&str]) -> BTreeSet<String> {
    v.iter().map(|s| s.to_string()).collect()
}

/// `(unit, host, items)` of each target.
fn targets(plan: &DeployPlan) -> Vec<(String, String, BTreeSet<String>)> {
    plan.targets
        .iter()
        .map(|t| (t.family.clone(), t.host.clone(), t.items.clone()))
        .collect()
}

#[test]
fn a_merge_resolution_service_and_a_stacked_edit_of_an_earlier_binary_both_ship_with_their_items() {
    let fx = Fx::new();
    let base = fx.base();
    let added = commit_on(
        &fx,
        &base,
        &[("rust/newbin/src/main.rs", "fn main() {}\n")],
        "add\n\nPlan-Item: demo:P1\nDeploy: alpha",
    );
    let edited = commit_on(
        &fx,
        &added,
        &[("rust/newbin/src/main.rs", "fn main() { run() }\n")],
        "edit\n\nPlan-Item: demo:P2\nDeploy: beta",
    );
    let p = fx.path();
    g(p, &["merge", "-q", "--no-ff", "--no-commit", &edited]);
    std::fs::create_dir_all(p.join("deploy/systemd")).unwrap();
    std::fs::write(p.join("deploy/systemd/res.service"), "[Service]\n").unwrap();
    g(p, &["add", "-A"]);
    g(
        p,
        &[
            "commit",
            "-q",
            "-m",
            "merge\n\nPlan-Item: demo:M\nDeploy: gamma",
        ],
    );
    let head = g(p, &["rev-parse", "HEAD"]);
    let parts = derive_landed(p, &base, &head).unwrap();
    let plan = plan_with_roles(
        &parts,
        &BTreeMap::new(),
        &set(&["alpha", "beta", "gamma"]),
        &RoleHosts::new(),
        &NoProbe,
    );
    assert_eq!(
        targets(&plan),
        [
            ("bin:newbin".into(), "alpha".into(), set(&["demo:P1"])),
            ("bin:newbin".into(), "beta".into(), set(&["demo:P2"])),
            ("res.service".into(), "gamma".into(), set(&["demo:M"])),
        ]
    );
    assert!(plan.blockers.is_empty(), "{plan:?}");
}

#[test]
fn a_stacked_train_part_that_edits_a_binary_its_parent_added_ships_it_with_its_own_item_and_host() {
    let fx = Fx::new();
    let base = fx.base();
    let added = commit_on(
        &fx,
        &base,
        &[("rust/newbin/src/main.rs", "fn main() {}\n")],
        "add\n\nPlan-Item: demo:P1\nDeploy: alpha",
    );
    let edited = commit_on(
        &fx,
        &added,
        &[("rust/newbin/src/main.rs", "fn main() { run() }\n")],
        "edit\n\nPlan-Item: demo:P2\nDeploy: beta",
    );
    let input = BuildInput {
        repo: fx.path(),
        build: "b1".into(),
        base,
        parts: vec![part(1, &added, None), part(2, &edited, Some((1, &added)))],
        attributes: String::new(),
        cap: None,
        resolutions: vec![],
        protected: vec![],
    };
    let manifest = build(&input).unwrap().manifest.unwrap();
    let parts = derive(fx.path(), &manifest).unwrap();
    assert_eq!(parts[1].families, set(&["bin:newbin"]));
    let plan = deploy_plan(&parts, &BTreeMap::new(), &set(&["alpha", "beta"]), &NoProbe);
    assert_eq!(
        targets(&plan),
        [
            ("bin:newbin".into(), "alpha".into(), set(&["demo:P1"])),
            ("bin:newbin".into(), "beta".into(), set(&["demo:P2"])),
        ]
    );
    assert_eq!(plan.targets[1].parts, [2]);
}

const MANIFEST: &str = r#"
[[unit]]
name = "pg-x"
files = ["pg-x.service", "pg-x.timer"]
hosts = ["keeper"]
first_run = "it runs"

[[unit]]
name = "pg-sys"
files = ["pg-sys.service"]
scope = "system"
hosts = ["keeper"]
first_run = "it runs"

[[unit]]
name = "pg-off"
files = ["pg-off.service"]
not_deployed = "later"

[[binary]]
package = "app"
bin = "app"
hosts = ["all"]
first_run = "it prints"

[[binary]]
package = "app"
bin = "tool"
hosts = ["worker"]
first_run = "it prints"
"#;

fn index() -> Index {
    let tomls = BTreeMap::from([
        ("core".to_string(), "[package]\nname = \"core-lib\"\n".to_string()),
        (
            "app".to_string(),
            "[package]\nname = \"app\"\n[package.metadata.plangraph]\nhost-binaries = [\"app\"]\n[dependencies]\ncore-lib = { path = \"../core\" }\n".to_string(),
        ),
    ]);
    Index {
        manifest: Some(Manifest::parse(MANIFEST).unwrap()),
        crates: Crates::parse(&tomls),
    }
}

fn names(index: &Index, status: &str, path: &str) -> Vec<(String, Vec<String>, Option<String>)> {
    index
        .families_for(status, path)
        .into_iter()
        .map(|f| (f.name, f.units, f.skip))
        .collect()
}

#[test]
fn manifest_families_follow_unit_files_and_crate_dependents() {
    let ix = index();
    let unit = ix.families_for("M", "deploy/systemd/pg-x.service");
    assert_eq!(unit[0].name, "pg-x");
    assert_eq!(unit[0].units, ["pg-x.timer", "pg-x.service"]);
    assert_eq!(unit[0].roles, ["keeper"]);
    assert_eq!(unit[0].skip, None);
    let bins = names(&ix, "M", "rust/core/src/lib.rs");
    assert_eq!(bins.len(), 2);
    assert_eq!(bins[0], ("bin:app".into(), vec!["bin:app".into()], None));
    assert!(
        bins[1].2.as_deref().unwrap().contains("host-binaries"),
        "{bins:?}"
    );
    assert!(names(&ix, "D", "rust/core/src/lib.rs").is_empty());
    assert!(names(&ix, "M", "docs/readme.md").is_empty());
    assert!(names(&ix, "M", "deploy/systemd/pg-sys.service")[0]
        .2
        .as_deref()
        .unwrap()
        .starts_with("system unit"));
    assert_eq!(
        names(&ix, "A", "deploy/systemd/pg-off.service")[0]
            .2
            .as_deref(),
        Some("not deployed: later")
    );
}

#[test]
fn manifest_roles_pick_the_hosts_and_skipped_families_carry_their_reason() {
    let ix = index();
    let mut pd = PartDeploy {
        items: set(&["demo:K"]),
        ..PartDeploy::default()
    };
    for path in [
        "deploy/systemd/pg-x.timer",
        "rust/app/src/main.rs",
        "deploy/systemd/pg-off.service",
    ] {
        for family in ix.families_for("M", path) {
            pd.add(&family);
        }
    }
    let roles = parse_role_hosts("k=keeper,w=worker+wsl").unwrap();
    let plan = plan_with_roles(&[pd], &BTreeMap::new(), &set(&["k", "w"]), &roles, &NoProbe);
    assert_eq!(
        plan.go_req(),
        "deploy: bin:app@k, bin:app@w, pg-x.service@k, pg-x.timer@k"
    );
    assert_eq!(
        plan.skipped.keys().collect::<Vec<_>>(),
        ["bin:tool", "pg-off"]
    );
    assert!(parse_role_hosts("k=chef").is_err());
}
