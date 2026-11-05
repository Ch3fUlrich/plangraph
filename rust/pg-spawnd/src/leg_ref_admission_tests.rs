use super::*;
use crate::placement::{route_args, Args};
use serde_json::json;
fn args(value: serde_json::Value) -> Args {
    value.as_object().unwrap().clone()
}
fn catalog() -> Vec<LegRef> {
    vec![LegRef {
        id: "registered".into(),
        model: "provider/model".into(),
        roles: vec!["implement".into(), "review".into()],
    }]
}
#[test]
fn off_and_warn_preserve_existing_route_payload() {
    for mode in [LegRefMode::Off, LegRefMode::Warn] {
        for reference in ["provider/model", "--invalid"] {
            let mut a = args(json!({"card":{}, "model": reference}));
            let original = a.clone();
            assert!(admit_ref(&mut a, &catalog(), mode).is_ok());
            assert_eq!(a, original);
            assert_eq!(route_args(&a).ok(), route_args(&original).ok());
        }
    }
}
#[test]
fn model_ref_canonicalized_only_on() {
    let mut a = args(json!({"model":"provider/model"}));
    assert!(admit_ref(&mut a, &catalog(), LegRefMode::On).is_ok());
    assert_eq!(a["leg"], "registered");
    assert!(!a.contains_key("model"));
}
#[test]
fn conflicting_selectors_refused() {
    let mut c = catalog();
    c.push(LegRef {
        id: "other".into(),
        model: "other-model".into(),
        roles: vec!["implement".into()],
    });
    let mut a = args(json!({"model":"provider/model", "leg":"other"}));
    let original = a.clone();
    assert!(admit_ref(&mut a, &c, LegRefMode::On).is_err());
    assert_eq!(a, original);
}
#[test]
fn diff_ref_implement_refused_naming_review() {
    for mode in [LegRefMode::Off, LegRefMode::Warn, LegRefMode::On] {
        for value in [
            json!({"role":"implement", "card":{"diff_ref":"change"}}),
            json!({"diff_ref":"change"}),
            json!({"card":{"diff_ref":false}, "dry_run":true}),
        ] {
            let mut a = args(value);
            let error = admit_ref(&mut a, &catalog(), mode)
                .err()
                .unwrap()
                .into_error_data();
            assert_eq!(
                error.message,
                "diff_ref requires role review; role implement is refused"
            );
            assert!(route_args(&a).is_err());
        }
    }
}
#[test]
fn diff_ref_review_allowed() {
    assert!(check_review_role(&args(
        json!({"role":"review", "card":{"diff_ref":"change"}})
    ))
    .is_ok());
}
#[test]
fn null_or_absent_diff_ref_unchanged() {
    for value in [
        json!({}),
        json!({"diff_ref":null, "card":{"diff_ref":null}}),
    ] {
        assert!(check_review_role(&args(value)).is_ok());
    }
}
#[test]
fn parameter_error_wire_shape_preserved() {
    let mut a = args(json!({"leg":"--invalid"}));
    let error = admit_ref(&mut a, &catalog(), LegRefMode::On)
        .err()
        .unwrap()
        .into_error_data();
    assert_eq!(error.code.0, -32602);
    let data = error.data.unwrap();
    assert_eq!(data["type"], "ParameterRefused");
    assert_eq!(data["exit_code"], 2);
}

#[cfg(unix)]
#[test]
fn role_rule_precedes_adapter_and_dry_run() {
    // Isolate the adapter environment from all concurrently running tests.
    const CHILD: &str = "PG_LEG_REF_ROLE_TEST";
    if std::env::var_os(CHILD).is_some() {
        let state = std::env::var_os("PG_LEG_REF_STATE").unwrap();
        for dry_run in [false, true] {
            for mut a in [
                args(json!({"card":{"diff_ref":"change"}})),
                args(json!({"card":{}, "diff_ref":"change"})),
            ] {
                a.insert("dry_run".into(), json!(dry_run));
                let error = crate::placement::route(Path::new(&state), &a)
                    .unwrap_err()
                    .into_error_data();
                assert_eq!(
                    error.message,
                    "diff_ref requires role review; role implement is refused"
                );
            }
        }
        return;
    }
    use std::os::unix::fs::PermissionsExt;
    let temp = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&temp).unwrap();
    let script = temp.join("adapter.sh");
    let marker = temp.join("called");
    std::fs::write(
        &script,
        "#!/bin/sh\ntouch \"$PG_LEG_REF_MARKER\"\nprintf '{}'\n",
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "leg_ref::admission_tests::role_rule_precedes_adapter_and_dry_run",
        ])
        .env(CHILD, "1")
        .env("PG_LEG_REF_STATE", &temp)
        .env("PG_LEG_REF_MARKER", &marker)
        .env("PG_SPAWND_ADAPTER", &script)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert!(String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line.starts_with("test result: ok. 1 passed; 0 failed;")));
    let called = marker.exists();
    std::fs::remove_dir_all(&temp).unwrap();
    assert!(!called, "refusal must not invoke the adapter");
}
