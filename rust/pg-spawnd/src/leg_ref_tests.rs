use super::*;
#[test]
fn synthetic_34_mutations_suggest_or_refuse_never_autocorrect() {
    // 25 harvested spellings, verbatim; not a live historical replay.
    let harvested = [
        "agy-gemini-3.8-flash",
        "agy/gemini-3.8-flash",
        "anthropic/claude-haiku-5-5",
        "claude-haiku-5-5",
        "claude/claude-haiku-5-5",
        "cx-gpt-6-luna",
        "glm-5.3-flash-free",
        "omniroute/cx-gpt-6-luna",
        "omniroute/cx-gpt-6.1-terra",
        "omniroute/gc/grok-4.7",
        "omniroute/or-laguna-s-2.1-free",
        "omniroute/or-nemotron-3-super-free",
        "omniroute/or-north-mini-code-free",
        "omniroute/ovh-gpt-oss-120b",
        "opencode-glm-5.3-flash-free",
        "opencode-go-kimi",
        "opencode/claude-haiku-5-5",
        "opencode/flint-40b-free",
        "opencode/muse-spark-1.3-contributor-free",
        "opencode/muse-spark-1.3-free",
        "opencode/nemotron-3-super-free",
        "opencode/nemotron-3-ultra-free",
        "or-nemotron-3-super-free",
        "qodercli-qwen3.8-flash",
        "qodercli/qwen3.8-flash",
    ];
    // Nine labelled synthetic one-character mutations of literal catalog ids.
    let synthetic = [
        "alph", "alphax", "bet", "betax", "delt", "deltax", "gamm", "gammax", "thet",
    ];
    assert_eq!(harvested.len() + synthetic.len(), 34);
    for reference in harvested.into_iter().chain(synthetic) {
        assert!(
            matches!(resolve_leg(reference, "implement", &catalog()), Err(RefError::Unknown(hints)) if hints.len() <= 3)
        );
    }
}
fn catalog() -> Vec<LegRef> {
    [
        ("alpha", "provider/model"),
        ("beta", "shared"),
        ("delta", "shared"),
        ("gamma", "shared"),
        ("theta", "shared"),
    ]
    .into_iter()
    .map(|(id, model)| LegRef {
        id: id.into(),
        model: model.into(),
        roles: vec!["implement".into()],
    })
    .collect()
}
#[test]
fn exact_id_and_unique_model_resolve() {
    for reference in ["alpha", "provider/model"] {
        assert_eq!(
            resolve_leg(reference, "implement", &catalog()),
            Ok("alpha".into())
        );
    }
}
#[test]
fn exact_id_precedes_model_collision() {
    let mut c = catalog();
    c[1].model = "alpha".into();
    assert_eq!(resolve_leg("alpha", "implement", &c), Ok("alpha".into()));
}
#[test]
fn ambiguous_model_refuses_sorted_three() {
    assert_eq!(
        resolve_leg("shared", "implement", &catalog()),
        Err(RefError::Ambiguous(vec![
            "beta".into(),
            "delta".into(),
            "gamma".into()
        ]))
    );
}
#[test]
fn typo_suggestions_distance_ties_limit_three() {
    assert_eq!(
        resolve_leg("alph", "implement", &catalog()),
        Err(RefError::Unknown(vec!["alpha".into()]))
    );
    if let Err(RefError::Unknown(hints)) = resolve_leg("eta", "implement", &catalog()) {
        assert!(hints.len() <= 3);
    } else {
        panic!()
    }
}
#[test]
fn unrelated_ref_clean_refusal() {
    assert_eq!(
        resolve_leg("unrelated-reference", "implement", &catalog()),
        Err(RefError::Unknown(vec![]))
    );
}
#[test]
fn invalid_empty_option_whitespace_control_too_long() {
    for s in ["", "--alpha", "a b", "a\n", "a\u{7f}", &"a".repeat(201)] {
        assert_eq!(
            resolve_leg(s, "implement", &catalog()),
            Err(RefError::Invalid)
        );
    }
}
#[test]
fn case_sensitive_match_casefolded_suggestions() {
    assert_eq!(
        resolve_leg("ALPHA", "implement", &catalog()),
        Err(RefError::Unknown(vec!["alpha".into()]))
    );
}
#[test]
fn role_ineligible_refused() {
    assert_eq!(
        resolve_leg("alpha", "review", &catalog()),
        Err(RefError::Role)
    );
    assert_eq!(
        resolve_leg("alpha", "unknown", &catalog()),
        Err(RefError::Role)
    );
}
#[test]
fn duplicate_catalog_rows_deduped_or_ambiguous() {
    let mut c = catalog();
    c.push(c[0].clone());
    assert_eq!(
        resolve_leg("provider/model", "implement", &c),
        Ok("alpha".into())
    );
}
#[test]
fn switches_env_file_default_per_role_hot_reload_warn_log_only() {
    let path = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    assert_eq!(mode(None, &path, "review"), LegRefMode::Off);
    std::fs::write(&path, r#"{"leg_ref":{"review":"on"}}"#).unwrap();
    assert_eq!(mode(None, &path, "review"), LegRefMode::On);
    assert_eq!(mode(Some("warn"), &path, "review"), LegRefMode::Warn);
    std::fs::write(&path, "malformed").unwrap();
    assert_eq!(mode(None, &path, "review"), LegRefMode::Off);
    assert_eq!(mode(Some("invalid"), &path, "review"), LegRefMode::Off);
    std::fs::remove_file(path).unwrap();
}
