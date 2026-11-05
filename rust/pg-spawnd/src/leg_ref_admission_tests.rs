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
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout)
        .lines()
        .any(|line| line.starts_with("test result: ok. 1 passed; 0 failed;")));
    let called = marker.exists();
    std::fs::remove_dir_all(&temp).unwrap();
    assert!(!called, "refusal must not invoke the adapter");
}

#[cfg(unix)]
fn wired_adapter_case(invalid: bool) {
    const CHILD: &str = "PG_LEG_REF_WIRED";
    if std::env::var_os(CHILD).is_some() {
        use pg_worker_core::run::{
            group::Confinement,
            supervise::Config,
            supervisor::{Supervisor, SupervisorCfg},
        };
        let state = std::path::PathBuf::from(std::env::var_os("PG_LEG_REF_STATE").unwrap());
        let a = args(
            json!({"card":{"task":"test"}, "prompt":"test", "model":if invalid {"--bad"} else {"provider/model"}, "dry_run":true}),
        );
        let route = crate::placement::route(&state, &a);
        if !invalid {
            struct FakeCatalog;
            impl LegCatalog for FakeCatalog {
                fn legs(&self) -> Result<Vec<LegRef>, crate::errors::ToolError> {
                    Ok(catalog())
                }
            }
            assert_eq!(admit(&FakeCatalog, &a).ok().unwrap()["leg"], "registered");
        }
        let runs = crate::runs::Runs::new(std::sync::Arc::new(
            Supervisor::new(SupervisorCfg::new(Config {
                state_dir: state.clone(),
                confinement: Confinement::Plain,
                ..Config::default()
            }))
            .unwrap(),
        ));
        let spawn = crate::spawn::spawn(&state, &a, "local", &runs);
        assert_eq!(route.is_err(), invalid);
        assert_eq!(spawn.is_err(), invalid);
        let rows = crate::records::list_runs(&state);
        assert_eq!(rows.len(), 1);
        if invalid {
            assert_eq!(rows[0]["refusal"], "ParameterRefused");
        } else {
            assert_eq!(rows[0]["host"], "local");
            assert_eq!(rows[0]["chosen_leg"]["id"], "planner-choice");
        }
        return;
    }
    use std::os::unix::fs::PermissionsExt;
    let temp = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    std::fs::create_dir(&temp).unwrap();
    let script = temp.join("adapter.sh");
    std::fs::write(
        &script,
        r#"#!/bin/sh
printf '%s\n' "$*" >> "$PG_LEG_REF_STATE/calls"
case "$3" in
catalog) printf '[{"id":"registered","model":"provider/model","roles":["implement"]}]';;
route) printf '{"legs":[{"id":"registered","harness":"test","model":"provider/model","family":"test"}],"first_leg":{"id":"planner-choice","harness":"test","model":"provider/planned","family":"test"}}';;
*) exit 9;;
esac
"#,
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700)).unwrap();
    let launch = temp.join("launch.sh");
    std::fs::write(
        &launch,
        "#!/bin/sh\nprintf '{\"argv\":[\"true\"],\"cwd\":\".\",\"env\":{},\"stdin\":\"test\",\"prompt\":\"test\"}'\n",
    )
    .unwrap();
    std::fs::set_permissions(&launch, std::fs::Permissions::from_mode(0o700)).unwrap();
    let name = if invalid {
        "invalid_ref_never_calls_adapter_in_on"
    } else {
        "mcp_route_and_spawn_admission_preserves_planner_choice"
    };
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", &format!("leg_ref::admission_tests::{name}")])
        .env(CHILD, "1")
        .env("PG_LEG_REF_STATE", &temp)
        .env("PLANGRAPH_RELIABILITY_LEG_REF", "on")
        .env("PG_SPAWND_LAUNCH_CMD", &launch)
        .env("PG_SPAWND_ADAPTER", &script)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let calls = std::fs::read_to_string(temp.join("calls")).unwrap_or_default();
    if invalid {
        assert!(calls.is_empty());
    } else {
        assert_eq!(calls.lines().filter(|l| l.ends_with("catalog")).count(), 1);
        let route = calls.lines().find(|l| l.contains(" route ")).unwrap();
        assert!(!route.contains("--leg"));
        assert!(!calls.lines().any(|l| l.contains(" spawn ")));
    }
    std::fs::remove_dir_all(temp).unwrap();
}
#[cfg(unix)]
#[test]
fn mcp_route_and_spawn_admission_preserves_planner_choice() {
    wired_adapter_case(false);
}
#[cfg(unix)]
#[test]
fn invalid_ref_never_calls_adapter_in_on() {
    wired_adapter_case(true);
}

#[test]
fn omitted_role_with_diff_ref_refused() {
    for mode in [LegRefMode::Off, LegRefMode::Warn, LegRefMode::On] {
        let mut a = args(json!({"card":{"diff_ref":"change"},"dry_run":true}));
        assert!(admit_ref(&mut a, &catalog(), mode).is_err());
    }
}

#[test]
fn probe_role_rule_precedes_adapter() {
    for value in [
        json!({"card":{"diff_ref":"change"}, "dry_run":true}),
        json!({"role":"implement", "diff_ref":"change"}),
    ] {
        let error = crate::placement::probe(Path::new("unused"), &args(value))
            .unwrap_err()
            .into_error_data();
        assert_eq!(
            error.message,
            "diff_ref requires role review; role implement is refused"
        );
    }
}

#[test]
fn filtered_empty_suggestions_omit_suffix() {
    let c = vec![LegRef {
        id: "unsafe!".into(),
        model: "provider/model".into(),
        roles: vec!["implement".into()],
    }];
    let mut a = args(json!({"model":"provider/mode"}));
    let error = admit_ref(&mut a, &c, LegRefMode::On)
        .unwrap_err()
        .into_error_data();
    assert_eq!(error.message, "unknown leg/reference");
}

#[test]
fn diff_ref_refuses_every_non_review_role() {
    for role in ["orchestrate", "unknown", ""] {
        for mode in [LegRefMode::Off, LegRefMode::Warn, LegRefMode::On] {
            for value in [
                json!({"role":role,"diff_ref":"change"}),
                json!({"role":role,"card":{"diff_ref":"change"},"dry_run":true}),
            ] {
                assert!(admit_ref(&mut args(value), &catalog(), mode).is_err());
            }
        }
    }
}

#[test]
fn ambiguous_diagnostic_is_distinct_from_unknown() {
    let mut rows = catalog();
    rows.push(LegRef {
        id: "another".into(),
        ..rows[0].clone()
    });
    let error = admit_ref(
        &mut args(json!({"model":"provider/model"})),
        &rows,
        LegRefMode::On,
    )
    .unwrap_err()
    .into_error_data();
    assert_eq!(
        error.message,
        "ambiguous reference; candidates: another, registered"
    );
}

#[test]
fn ambiguous_filtered_candidates_omit_suffix() {
    let rows: Vec<_> = ["unsafe!a", "unsafe!b"]
        .into_iter()
        .map(|id| LegRef {
            id: id.into(),
            model: "shared".into(),
            roles: vec!["implement".into()],
        })
        .collect();
    let error = admit_ref(&mut args(json!({"model":"shared"})), &rows, LegRefMode::On)
        .unwrap_err()
        .into_error_data();
    assert_eq!(error.message, "ambiguous reference");
}

#[test]
fn warn_catalog_failure_logs_once() {
    const CHILD: &str = "PG_LEG_REF_WARN_ONCE_TEST";
    if std::env::var_os(CHILD).is_some() {
        struct Failed;
        impl LegCatalog for Failed {
            fn legs(&self) -> Result<Vec<LegRef>, crate::errors::ToolError> {
                Err(crate::placement::parameter("catalog unavailable"))
            }
        }
        let a = args(json!({"model":"shared"}));
        for _ in 0..2 {
            assert_eq!(admit(&Failed, &a).ok().unwrap(), a);
        }
        return;
    }
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "leg_ref::admission_tests::warn_catalog_failure_logs_once",
            "--nocapture",
        ])
        .env(CHILD, "1")
        .env("PLANGRAPH_RELIABILITY_LEG_REF", "warn")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert_eq!(
        String::from_utf8_lossy(&output.stderr)
            .matches("leg_ref: catalog unavailable (observation only)")
            .count(),
        1
    );
}
