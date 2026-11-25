//! The binding of a steps file to the inputs and run it was started with.
use super::*;

fn drive_with(args: &Args, die_on: Option<usize>) -> Result<i32, String> {
    let mut calls = Calls(Vec::new(), die_on);
    drive(args, None, &mut calls, &mut Vec::new())
}

#[test]
fn a_claim_only_prefix_is_bound_to_the_inputs_it_was_started_with() {
    let dir = tempfile::tempdir().unwrap();
    let args = Args {
        journal: false,
        ..parse(&call(dir.path())).unwrap()
    };
    // Killed during the worktree step: the steps file holds the claim row alone, which carries
    // neither the base nor the brief.
    let killed =
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| drive_with(&args, Some(1))));
    assert!(killed.is_err());
    assert_eq!(rows_saved(dir.path()), 1);
    for (what, edit) in [
        (
            "base",
            (|a: &mut Args| a.inputs.base = HEAD.into()) as fn(&mut Args),
        ),
        ("run_id", |a| a.run_id = "run-other".into()),
        ("brief", |a| a.inputs.brief = "/elsewhere/brief.md".into()),
    ] {
        let mut other = Args {
            journal: false,
            ..parse(&call(dir.path())).unwrap()
        };
        edit(&mut other);
        let error = drive_with(&other, None).unwrap_err();
        assert!(error.contains(&format!("other {what}")), "{what}: {error}");
    }
    // The same inputs resume.
    assert_eq!(drive_with(&args, None).unwrap(), 0);
}

#[test]
fn a_sidecar_is_never_the_steps_file_itself() {
    // `with_extension("claim-key")` of `run.claim-key` is `run.claim-key`: the key would be
    // read back as step JSON.
    let steps = Path::new("/state/run.claim-key");
    for suffix in [".claim-key", ".findings", ".inputs"] {
        let side = sidecar(steps, suffix);
        assert_ne!(side, steps);
        assert_eq!(side.parent(), steps.parent());
    }
    assert_eq!(
        sidecar(Path::new("/state/steps.json"), ".claim-key"),
        Path::new("/state/steps.json.claim-key")
    );
}

#[test]
fn two_runs_on_one_steps_path_cannot_both_establish_the_binding() {
    let dir = tempfile::tempdir().unwrap();
    let base = Args {
        journal: false,
        ..parse(&call(dir.path())).unwrap()
    };
    let results: Vec<Result<(), String>> = std::thread::scope(|scope| {
        let handles: Vec<_> = (0..8)
            .map(|n| {
                let args = Args {
                    run_id: format!("run-{n}"),
                    ..base.clone()
                };
                scope.spawn(move || bind_inputs(&args, None))
            })
            .collect();
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    assert_eq!(
        results.iter().filter(|r| r.is_ok()).count(),
        1,
        "{results:?}"
    );
    assert!(results
        .iter()
        .filter_map(|r| r.as_ref().err())
        .all(|e| e.contains("other run_id")));
}
