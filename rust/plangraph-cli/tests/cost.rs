use chrono::{TimeZone, Utc};
use plangraph_cli::cost::{self, Config};
use serde_json::json;
fn config() -> Config {
    let mut cfg: Config = toml::from_str("warn_usd='1'\nbudget_usd='2'\nrows_file='/private/rows.json'\n[prices.'vertex/model']\ninput='0.000001'\noutput='0.000002'\ncache_read='0.0000001'\n").unwrap();
    cfg.rows_file = std::env::temp_dir().join("rows.json");
    cfg
}
fn export(input: u64) -> serde_json::Value {
    json!({"day":"2026-11-13","exported_at":"2026-11-13T12:00:00Z","truncated":false,
        "rows":[{"provider":"vertex","model":"model","timestamp":"2026-11-13T11:00:00Z","tokens":{"in":input,"out":0}}]})
}
fn now() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2026, 11, 13, 12, 0, 0).unwrap()
}
#[test]
fn ok_near_over_exact_boundary_and_cache() {
    for (input, expected) in [
        (0, "ok"),
        (999999, "ok"),
        (1000000, "near"),
        (1999999, "near"),
        (2000000, "over"),
        (2000001, "over"),
    ] {
        let verdict = cost::evaluate(&config(), &export(input), now()).unwrap();
        assert_eq!(verdict.verdict, expected);
        assert_eq!(verdict.admits(), expected != "over");
    }
    let mut rows = export(2000000);
    rows["rows"][0]["tokens"]["cacheRead"] = json!(2000000);
    assert_eq!(cost::evaluate(&config(), &rows, now()).unwrap().usd, "0.2");
}
#[test]
fn unpriced_truncated_invalid_stale_and_other_day() {
    let mut rows = export(1);
    rows["rows"][0]["model"] = json!("unknown");
    assert_eq!(
        cost::evaluate(&config(), &rows, now()).unwrap().verdict,
        "unpriced"
    );
    rows = export(1);
    rows["truncated"] = json!(true);
    assert_eq!(
        cost::evaluate(&config(), &rows, now()).unwrap().verdict,
        "truncated"
    );
    rows = export(1);
    rows["rows"][0]["tokens"]["in"] = json!("garbage");
    assert_eq!(
        cost::evaluate(&config(), &rows, now()).unwrap().verdict,
        "invalid"
    );
    assert!(cost::evaluate(
        &config(),
        &export(1),
        now() + chrono::Duration::seconds(601)
    )
    .is_err());
    rows = export(1);
    rows["day"] = json!("2026-11-12");
    assert!(cost::evaluate(&config(), &rows, now()).is_err());
    rows = export(2000000);
    rows["rows"][0]["timestamp"] = json!("2026-11-12T11:00:00Z");
    assert_eq!(
        cost::evaluate(&config(), &rows, now()).unwrap().verdict,
        "ok"
    );
}
#[test]
fn invalid_config_and_decimal_fraction_are_never_rounded_into_admission() {
    let mut cfg = config();
    cfg.budget_usd = "0.00000001".into();
    cfg.warn_usd = "0".into();
    cfg.prices.get_mut("vertex/model").unwrap().input = "0.000000010001".into();
    assert_eq!(
        cost::evaluate(&cfg, &export(1), now()).unwrap().verdict,
        "over"
    );
    cfg.budget_usd = "NaN".into();
    assert!(cost::evaluate(&cfg, &export(1), now()).is_err());
}
#[test]
fn malformed_token_blocks_never_become_free_calls() {
    for tokens in [json!("garbage"), json!(null), json!([])] {
        let mut rows = export(1);
        rows["rows"][0]["tokens"] = tokens;
        assert_eq!(
            cost::evaluate(&config(), &rows, now()).unwrap().verdict,
            "invalid"
        );
    }
}
#[test]
fn explicit_paid_provider_prices_and_unrepresentable_decimal() {
    let mut cfg = config();
    let price = cfg.prices.remove("vertex/model").unwrap();
    cfg.prices.insert("openrouter/model".into(), price);
    let mut rows = export(3000000);
    rows["rows"][0]["provider"] = json!("openrouter");
    assert_eq!(cost::evaluate(&cfg, &rows, now()).unwrap().verdict, "over");
    cfg = config();
    cfg.warn_usd = "0".into();
    cfg.budget_usd = "0.0000000000000000000000000001".into();
    cfg.prices.get_mut("vertex/model").unwrap().input = "0.00000000000000000000000000004".into();
    assert!(cost::evaluate(&cfg, &export(3), now()).is_err());
}
#[test]
fn migration_copy_once_and_overlap_minimum() {
    use plangraph_cli::cost_cutover;
    let root = tempfile::tempdir().unwrap();
    let old = root.path().join("old");
    let new = root.path().join("new");
    std::fs::create_dir(&old).unwrap();
    std::fs::write(old.join("daily-gate.json"), "{}").unwrap();
    assert_eq!(cost_cutover::import(&old, &new, true).unwrap(), 1);
    assert!(!new.exists());
    assert_eq!(cost_cutover::import(&old, &new, false).unwrap(), 1);
    assert_eq!(cost_cutover::import(&old, &new, false).unwrap(), 0);
    assert!(!cost_cutover::overlap(&new, true, true, now()).unwrap());
    assert!(!new.join("cost-gate-overlap-start").exists());
    assert!(!cost_cutover::overlap(&new, true, false, now()).unwrap());
    assert!(
        !cost_cutover::overlap(&new, false, false, now() + chrono::Duration::seconds(86399))
            .unwrap()
    );
    assert!(
        cost_cutover::overlap(&new, false, false, now() + chrono::Duration::seconds(86400))
            .unwrap()
    );
}
#[test]
fn real_cli_private_budget_refresh_admit_and_dry_run() {
    use std::{fs, process::Command};
    let root = tempfile::tempdir().unwrap();
    let rows_path = root.path().join("rows.json");
    let config_path = root.path().join("gate.toml");
    let state = root.path().join("state");
    let text = format!("warn_usd='1'\nbudget_usd='2'\nrows_file={}\n[prices.'vertex/model']\ninput='0.000001'\noutput='0.000002'\ncache_read='0.0000001'\n",serde_json::to_string(&rows_path).unwrap());
    fs::write(&config_path, text).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&config_path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fs::write(
        root.path().join("env"),
        format!("PLANGRAPH_COST_CONFIG={}\n", config_path.display()),
    )
    .unwrap();
    let invoke = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .args(args)
            .env("HOME", root.path())
            .env("XDG_STATE_HOME", &state)
            .env("PLANGRAPH_ENV_FILE", root.path().join("env"))
            .env_remove("PLANGRAPH_COST_CONFIG")
            .output()
            .unwrap()
    };
    let fresh = |input| {
        let mut rows = export(input);
        let stamp = Utc::now();
        rows["day"] = json!(stamp.format("%Y-%m-%d").to_string());
        rows["exported_at"] = json!(stamp.to_rfc3339());
        rows["rows"][0]["timestamp"] = json!(stamp.to_rfc3339());
        rows
    };
    fs::write(&rows_path, fresh(1).to_string()).unwrap();
    assert!(invoke(&["cost-gate", "admit", "--dry-run"])
        .status
        .success());
    assert!(!state.exists());
    assert!(invoke(&["cost-gate", "admit"]).status.success());
    assert!(invoke(&["cost-gate", "status"]).status.success());
    fs::write(&rows_path, fresh(2000000).to_string()).unwrap();
    let before = fs::read(state.join("plangraph/daily-gate.json")).unwrap();
    assert_eq!(
        invoke(&["cost-gate", "admit", "--dry-run"]).status.code(),
        Some(1)
    );
    assert_eq!(
        fs::read(state.join("plangraph/daily-gate.json")).unwrap(),
        before
    );

    assert_eq!(invoke(&["cost-gate", "admit"]).status.code(), Some(1));
    fs::write(&rows_path, "{truncated").unwrap();
    let output = invoke(&["cost-gate", "admit"]);
    assert_eq!(output.status.code(), Some(2));
    assert!(!invoke(&["cost-gate", "status"]).status.success());
    assert!(!String::from_utf8_lossy(&output.stderr).contains(&root.path().display().to_string()));
}
#[test]
fn changed_gateway_token_schema_cannot_look_like_zero_spend() {
    let mut rows = export(1);
    rows["rows"][0]["tokens"] = json!({"input_tokens":3000000,"output_tokens":0});
    assert_eq!(
        cost::evaluate(&config(), &rows, now()).unwrap().verdict,
        "invalid"
    );
}
