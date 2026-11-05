use super::*;
const CAP: &str = "PG_RULE_HOST_WORKER_CAP_COUNT";

#[test]
fn schema_roundtrip_and_unknown_syntax_refused() {
    let specs = schema().unwrap();
    let json = serde_json::to_string(&specs).unwrap();
    assert_eq!(specs, serde_json::from_str::<Vec<RuleSpec>>(&json).unwrap());
    let text = include_str!("../../../setup/placement-rules.schema.toml");
    for suffix in ["\nextra = 1", "\nmax = 2", "\n[other]", "\nalarm = -1"] {
        assert!(parse_schema(&format!("{text}{suffix}")).is_err());
    }
    for bad in [
        text.replace("version = 1", "version = 2"),
        text.replace("default = 6", "default = 1025"),
        text.replace("kind = \"u64\"", "kind = \"float\""),
    ] {
        assert!(parse_schema(&bad).is_err());
    }
}

#[test]
fn seeds_match_memory_alarm_and_host_caps() {
    let specs = schema().unwrap();
    assert_eq!(specs[0].default, 6144);
    assert_eq!(specs[0].alarm, Some(3000));
    assert_eq!(specs[1].alarm, None);
    for (class, cap) in [("", 6), ("dispatch_vm", 4), ("prox", 5), ("unknown", 6)] {
        let r = resolve(&specs, class, &RuleLayers::default());
        assert_eq!(r.values[CAP], cap);
        assert_eq!(r.values["PG_RULE_HOST_MEMORY_FLOOR_MIB"], 6144);
        assert_eq!(r.sources[CAP], "default");
    }
    // These are policy values only: resolve has no Caps input or mutation.
}

fn boundary(level: usize) {
    let mut layers = RuleLayers::default();
    for (i, map) in [
        &mut layers.process,
        &mut layers.env_file,
        &mut layers.keeper_host,
        &mut layers.keeper_fleet,
    ]
    .into_iter()
    .enumerate()
    .skip(level)
    {
        map.insert(CAP.into(), (10 + i).to_string());
    }
    let r = resolve(&schema().unwrap(), "", &layers);
    assert_eq!(r.values[CAP], 10 + level as u64);
    assert_eq!(
        r.sources[CAP],
        ["process", "env_file", "keeper_host", "keeper_fleet"][level]
    );
}
#[test]
fn process_over_env_file() {
    boundary(0);
}
#[test]
fn env_file_over_keeper_host() {
    boundary(1);
}
#[test]
fn keeper_host_over_fleet() {
    boundary(2);
}
#[test]
fn fleet_over_default() {
    boundary(3);
}

#[test]
fn each_invalid_level_falls_back_and_reports() {
    for level in 0..4 {
        let mut layers = RuleLayers::default();
        let maps = [
            &mut layers.process,
            &mut layers.env_file,
            &mut layers.keeper_host,
            &mut layers.keeper_fleet,
        ];
        for (i, map) in maps.into_iter().enumerate() {
            if i == level {
                map.insert(CAP.into(), "1025".into());
            }
            if i == level + 1 {
                map.insert(CAP.into(), "9".into());
            }
        }
        let r = resolve(&schema().unwrap(), "", &layers);
        assert_eq!(r.values[CAP], if level == 3 { 6 } else { 9 });
        assert_eq!(
            r.diagnostics,
            [format!(
                "{CAP} {} out_of_range",
                ["process", "env_file", "keeper_host", "keeper_fleet"][level]
            )]
        );
    }
}

#[test]
fn outside_allowlist_refused() {
    let mut specs = schema().unwrap();
    specs[1].overrides = vec!["keeper_fleet".into()];
    let layers = RuleLayers {
        process: [
            (CAP.into(), "100".into()),
            ("PG_RULE_OTHER_COUNT".into(), "2".into()),
            ("OTHER".into(), "3".into()),
        ]
        .into(),
        keeper_fleet: [(CAP.into(), "7".into())].into(),
        ..Default::default()
    };
    let r = resolve(&specs, "", &layers);
    assert_eq!(r.values[CAP], 7);
    assert_eq!(r.values.len(), 2);
    assert_eq!(r.diagnostics.len(), 2);
    assert!(r
        .diagnostics
        .iter()
        .any(|v| v.contains("disallowed_source")));
}

#[test]
fn malformed_negative_overflow_and_empty() {
    for bad in ["bad", "-1", "18446744073709551616", "", "+2", " 2"] {
        let layers = RuleLayers {
            process: [(CAP.into(), bad.into())].into(),
            ..Default::default()
        };
        let r = resolve(&schema().unwrap(), "", &layers);
        assert_eq!(r.values[CAP], 6);
        assert_eq!(r.diagnostics, [format!("{CAP} process invalid_unsigned")]);
    }
    assert_eq!(RuleError::Io.to_string(), "rules_io");
    assert_eq!(RuleError::Schema.to_string(), "rules_schema");
    assert_eq!(RuleError::InvalidOverride.to_string(), "rules_override");
}

#[test]
fn env_file_duplicates_refused_no_execution() {
    for text in [
        "A=1\nA=2",
        "export A=2",
        "A=$(echo nope)",
        "A=${OTHER}",
        "A=`echo nope`",
        "missing",
    ] {
        assert!(parse_env_file(text).is_err());
    }
    let text = format!("# comment\n\n {CAP} = 8 \n");
    assert_eq!(parse_env_file(&text).unwrap()[CAP], "8");
}

#[test]
fn serialized_specs_drive_ui_metadata() {
    let specs = schema().unwrap();
    let json = serde_json::to_value(&specs).unwrap();
    assert_eq!(json[0]["alarm"], 3000);
    assert_eq!(json[1]["host_seeds"]["prox"], 5);
    assert_eq!(json[1]["overrides"].as_array().unwrap().len(), 4);
    assert!(
        serde_json::to_value(resolve(&specs, "", &RuleLayers::default())).unwrap()["values"]
            .is_object()
    );
}
