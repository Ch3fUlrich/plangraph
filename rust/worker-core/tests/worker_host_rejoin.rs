use pg_worker_core::{
    package_placement::{choose, Candidate, Policy},
    worker_host::{Phase, Step, Store},
};
use tempfile::tempdir;

#[test]
fn a_long_outage_requires_all_onboarding_steps_before_placement() {
    let state = tempdir().unwrap();
    let host = Store::new(state.path(), "worker-a").unwrap();
    host.observe(60, false, None).unwrap();
    host.observe(120, false, None).unwrap();
    assert_eq!(host.phase().unwrap(), Phase::Unreachable);
    host.observe(661, true, None).unwrap();
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);

    let candidate = Candidate {
        id: "worker-a".into(),
        workstation: false,
        ssh: None,
        sample: None,
        readiness: None,
    };
    let policy = Policy {
        worker_host_state: Some(state.path().into()),
        ..Policy::default()
    };
    let choice = choose(&[candidate], &policy, 661);
    assert!(choice.host.is_none());
    assert_eq!(choice.refusals[0].reason, "worker host rejoining");

    let mut steps = Vec::new();
    let result = host.rejoin(662, |step| {
        steps.push(step);
        if step == Step::Install {
            Err("installed binary is stale".into())
        } else {
            Ok(serde_json::Value::Null)
        }
    });
    assert!(result.is_err());
    assert!(steps.contains(&Step::Sync));
    assert!(steps.contains(&Step::Runs));
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);

    let mut steps = Vec::new();
    host.rejoin(663, |step| {
        steps.push(step);
        Ok(serde_json::Value::Null)
    })
    .unwrap();
    assert_eq!(
        steps,
        [
            Step::Sync,
            Step::Install,
            Step::Runs,
            Step::Worktrees,
            Step::Capacity,
            Step::Legs
        ]
    );
    assert_eq!(host.phase().unwrap(), Phase::Ready);
}

#[test]
fn a_second_outage_invalidates_an_in_flight_rejoin() {
    let state = tempdir().unwrap();
    let host = Store::new(state.path(), "worker-a").unwrap();
    host.observe(60, false, None).unwrap();
    host.observe(661, true, None).unwrap();
    assert!(host
        .rejoin(662, |step| {
            if step == Step::Legs {
                host.observe(663, false, None).unwrap();
                host.observe(664, true, None).unwrap();
            }
            Ok(serde_json::Value::Null)
        })
        .is_err());
    assert_eq!(host.phase().unwrap(), Phase::Rejoining);
}
