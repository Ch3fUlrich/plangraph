use super::*;
use crate::launch::LaunchSpec;
use pg_worker_core::run::record::new_run_id;

#[test]
fn bound_review_retry_refuses_to_drop_dispatch_binding() {
    let state = tempfile::tempdir().unwrap();
    let head = "b".repeat(40);
    let binding = json!({"base_sha":"a".repeat(40),"head_sha":head,"diff_sha256":"c".repeat(64)});
    let previous = json!({"id":"first"});
    let cli_leg = json!({"id":"cli","harness":"claude","model":"claude-sonnet-5-5-high"});
    let inline_leg = json!({"id":"inline","harness":"opencode"});
    for (label, diff_ref, next_head, leg, group) in [
        (
            "scoped diff",
            json!({"base":"a".repeat(40),"paths":["src/lib.rs"]}),
            Some(head.clone()),
            cli_leg.clone(),
            None,
        ),
        (
            "missing base",
            json!({}),
            Some(head.clone()),
            cli_leg.clone(),
            None,
        ),
        (
            "inline transport",
            json!({"base":"a".repeat(40)}),
            None,
            inline_leg,
            None,
        ),
        (
            "split group",
            json!({"base":"a".repeat(40)}),
            Some(head.clone()),
            cli_leg,
            Some((1, 2, vec!["src/lib.rs".into()])),
        ),
    ] {
        let mut record = runs::create(
            state.path(),
            &new_run_id(),
            "fixture",
            json!({"role":"review","head_sha":head,"review_binding":binding,
                   "attempts":[{"leg":previous,"status":"failed"}]}),
            Status::Failed,
        )
        .ok()
        .unwrap();
        let before = record.to_value();
        let log = record.path().with_file_name("live.log");
        std::fs::write(&log, "first attempt").unwrap();
        let launch = LaunchSpec {
            argv: vec!["reviewer".into()],
            cwd: state.path().into(),
            env: Default::default(),
            stdin: Some("review".into()),
            prompt: Some("review".into()),
            warnings: None,
            events: None,
        };
        let args = json!({"card":{"diff_ref":diff_ref}});
        let result = advance(
            &mut record,
            args.as_object().unwrap().clone(),
            &previous,
            1,
            leg,
            (new_job_id(), launch, next_head),
            group,
        );
        let error = result
            .err()
            .unwrap_or_else(|| panic!("{label}: retry admitted"));
        assert_eq!(
            error.into_error_data().message,
            "CLI review diff binding changed between attempts",
            "{label}"
        );
        assert_eq!(record.to_value(), before, "{label}");
        assert_eq!(std::fs::read_to_string(&log).unwrap(), "first attempt");
        assert!(!log.with_file_name("prompt.txt").exists());
    }
}
