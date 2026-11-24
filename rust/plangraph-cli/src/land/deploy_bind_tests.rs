use super::build_tests::{g, Fx};
use super::deploy::{DeployPlan, Target};
use super::deploy_bind::{bind, Approved};
use super::deploy_cmd_tests::{approved, commit_at, landed, plan_of, Landed, GO};
use std::collections::BTreeSet;

fn bind_it(
    fx: &Fx,
    l: &Landed,
    plan: &DeployPlan,
    go: &str,
    head: &str,
) -> Result<(Vec<Target>, super::deploy_post::Gate), String> {
    bind(
        plan,
        &approved(l),
        go,
        fx.path(),
        head,
        "main",
        &plan.digest(),
    )
}

#[test]
fn bind_accepts_a_plan_the_landed_train_really_ships() {
    let fx = Fx::new();
    let l = landed(&fx);
    let (targets, gate) = bind_it(&fx, &l, &plan_of(&l), GO, &l.landed).unwrap();
    assert_eq!(targets.len(), 1);
    assert_eq!(gate.fresh_after, 1_767_225_600);
}

#[test]
fn bind_refuses_a_foreign_go_a_foreign_train_a_head_off_main_and_an_edited_plan() {
    let fx = Fx::new();
    let l = landed(&fx);
    let off_main = fx.commit(&l.train, "other.txt", "x\n");
    let plan = plan_of(&l);
    let e = bind_it(&fx, &l, &plan, "deploy: a.service@beta", &l.landed).unwrap_err();
    assert!(e.contains("is not this plan's"), "{e}");
    let mut nameless = plan_of(&l);
    nameless.train_sha.clear();
    let e = bind_it(&fx, &l, &nameless, GO, &l.landed).unwrap_err();
    assert!(e.contains("names no train"), "{e}");
    // A train that is not merged into the landed head (the landed head predates it).
    let e = bind_it(&fx, &l, &plan, GO, &l.base).unwrap_err();
    assert!(e.contains("is not merged into landed head"), "{e}");
    let e = bind_it(&fx, &l, &plan, GO, &off_main).unwrap_err();
    assert!(e.contains("is not on main"), "{e}");
    // The plan is for another train than the one the GO evidence approved.
    let other = Approved {
        train_sha: l.base.clone(),
        base: l.base.clone(),
    };
    let e = bind(
        &plan,
        &other,
        GO,
        fx.path(),
        &l.landed,
        "main",
        &plan.digest(),
    )
    .unwrap_err();
    assert!(e.contains("the approved GO is for"), "{e}");
    // An edited plan keeps its GO line but not its digest.
    let mut edited = plan_of(&l);
    edited.targets[0].items = ["demo:C".to_string()].into();
    let e = bind(
        &edited,
        &approved(&l),
        GO,
        fx.path(),
        &l.landed,
        "main",
        &plan.digest(),
    )
    .unwrap_err();
    assert!(e.contains("approved plan digest"), "{e}");
}

#[test]
fn bind_refuses_a_target_the_landed_train_does_not_ship() {
    let fx = Fx::new();
    let l = landed(&fx);
    // A unit absent from the train, with a matching plan, GO line and digest.
    let mut ghost = plan_of(&l);
    ghost.targets[0].family = "ghost.service".into();
    let e = bind_it(&fx, &l, &ghost, "deploy: ghost.service@alpha", &l.landed).unwrap_err();
    assert!(e.contains("is not shipped by train"), "{e}");
    // An item key no commit of the train carries, and one the train carries but that ships no unit.
    for key in ["demo:Z", "demo:C"] {
        let mut foreign = plan_of(&l);
        foreign.targets[0].items = [key.to_string()].into();
        let e = bind_it(&fx, &l, &foreign, GO, &l.landed).unwrap_err();
        assert!(e.contains("does not ship a.service@alpha"), "{key}: {e}");
    }
    // A unit the landed tree no longer has.
    g(fx.path(), &["rm", "-q", "deploy/systemd/a.service"]);
    let gone = commit_at(&fx, 1_767_225_700, &[], "remove unit");
    let e = bind_it(&fx, &l, &plan_of(&l), GO, &gone).unwrap_err();
    assert!(e.contains("absent from the landed tree"), "{e}");
}

#[test]
fn bind_withholds_a_preflight_refused_timer_in_the_gate() {
    let fx = Fx::new();
    let l = landed(&fx);
    let mut plan = plan_of(&l);
    plan.blockers.push(super::deploy::Blocker {
        key: "plangraph:LE-DEPLOY-BLOCKED-A-TIMER-ALPHA".into(),
        family: "a.timer".into(),
        host: Some("alpha".into()),
        parts: vec![1],
        reasons: vec!["missing env X".into()],
    });
    let (_, gate) = bind_it(&fx, &l, &plan, GO, &l.landed).unwrap();
    assert_eq!(
        gate.excluded,
        BTreeSet::from([("a.timer".to_string(), "alpha".to_string())])
    );
}
