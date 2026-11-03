use crate::errors::{refused_record_error, RefusalKind, ToolError};
use serde_json::json;

#[test]
fn exception_table() {
    let rows = [
        (
            ToolError::Value("bad".into()),
            -32602,
            "bad",
            json!({"type":"ValueError","exit_code":1}),
        ),
        (
            ToolError::Key("run_id".into()),
            -32602,
            "'run_id'",
            json!({"type":"KeyError","exit_code":1}),
        ),
        (
            ToolError::Type("bad".into()),
            -32602,
            "bad",
            json!({"type":"TypeError","exit_code":1}),
        ),
        (
            ToolError::Internal("bad".into()),
            -32603,
            "bad",
            json!({"type":"RuntimeError","exit_code":1}),
        ),
    ];
    for (error, code, message, data) in rows {
        let error = error.into_error_data();
        assert_eq!(error.code.0, code);
        assert_eq!(error.message, message);
        assert_eq!(error.data, Some(data));
    }
    for (kind, name, code, exit) in [
        (RefusalKind::Parameter, "ParameterRefused", -32602, 2),
        (RefusalKind::Spawn, "SpawnRefused", -32000, 2),
        (RefusalKind::Benchmark, "BenchmarkError", -32602, 3),
    ] {
        let error = ToolError::Refused {
            kind,
            message: "verbatim".into(),
            legs: vec!["leg".into()],
        }
        .into_error_data();
        assert_eq!(error.code.0, code);
        assert_eq!(error.message, "verbatim");
        assert_eq!(
            error.data,
            Some(json!({"type":name,"exit_code":exit,"legs":["leg"]}))
        );
    }
}

#[test]
fn refused_table() {
    let error = refused_record_error(
        &json!({"status":"refused","legs":"","run_id":"","error":false,"exit_code":null}),
    )
    .unwrap();
    assert_eq!(error.message, "benchmark rows missing");
    assert_eq!(
        error.data,
        Some(json!({"type":"SpawnRefused","exit_code":null,"legs":[],"run_id":null}))
    );
    for (refusal, code) in [
        ("ParameterRefused", -32602),
        ("BenchRequired", -32602),
        ("BenchDataError", -32602),
        ("other", -32000),
    ] {
        let error =
            refused_record_error(&json!({"status":"refused","refusal":refusal,"run_id":"run-a"}))
                .unwrap();
        assert_eq!(error.code.0, code);
        assert_eq!(error.message, "benchmark rows missing");
        assert_eq!(
            error.data,
            Some(json!({"type":refusal,"exit_code":2,"legs":[],"run_id":"run-a"}))
        );
    }
    let error = refused_record_error(
        &json!({"status":"refused","error":"reason","exit_code":3,"legs":["a"]}),
    )
    .unwrap();
    assert_eq!(error.message, "reason");
    assert_eq!(
        error.data,
        Some(json!({"type":"SpawnRefused","exit_code":3,"legs":["a"],"run_id":null}))
    );
    assert!(refused_record_error(&json!({"status":"completed"})).is_none());
    assert!(refused_record_error(&json!([])).is_none());
}
