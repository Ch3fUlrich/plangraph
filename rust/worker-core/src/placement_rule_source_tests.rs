use super::*;

const KEY: &str = "PG_RULE_HOST_WORKER_CAP_COUNT";
struct EmptyStore;
impl RuleStore for EmptyStore {
    fn read(&self) -> Result<RuleSnapshot, StoreError> {
        Ok(RuleSnapshot::default())
    }
    fn commit(&mut self, _: &str, _: &RuleEdit) -> Result<RuleAudit, StoreError> {
        unreachable!("read-only fixture")
    }
}

#[test]
fn placement_rule_adapter_schema_seeds_and_independent_utf8_lines() {
    let dir = tempfile::tempdir().unwrap();
    let mut source = RuleSource {
        cache_path: dir.path().join("missing-parent/cache"),
        env_file: Some(dir.path().join("env")),
        host_id: "fixture".into(),
        host_class: "dispatch_vm".into(),
    };
    let specs = schema().unwrap();
    let memory = specs.iter().find(|s| s.name == "HostMemoryFloor").unwrap();
    assert_eq!((memory.default, memory.alarm), (6144, Some(3000)));
    let worker = specs.iter().find(|s| s.name == "HostWorkerCap").unwrap();
    assert_eq!(
        worker.host_seeds.values().copied().collect::<Vec<_>>(),
        [4, 5]
    );
    for (class, seed) in worker
        .host_seeds
        .iter()
        .map(|(k, v)| (k.as_str(), *v))
        .chain([("", 6)])
    {
        source.host_class = class.into();
        let loaded = source.load_with_env(&EmptyStore, &BTreeMap::new()).unwrap();
        assert_eq!(loaded.report.values[KEY], seed);
        assert_eq!(loaded.source, "keeper");
    }
    fs::write(
        source.env_file.as_ref().unwrap(),
        [
            format!("{KEY}=9\n").as_bytes(),
            &[255, b'\n'],
            b"PG_RULE_HOST_MEMORY_FLOOR_MIB=7000\n",
        ]
        .concat(),
    )
    .unwrap();
    let loaded = source.load_with_env(&EmptyStore, &BTreeMap::new()).unwrap();
    assert_eq!(loaded.report.values[KEY], 9);
    assert_eq!(loaded.report.values["PG_RULE_HOST_MEMORY_FLOOR_MIB"], 7000);
    assert_eq!(loaded.report.diagnostics, ["env_file line 2 invalid_utf8"]);
}

#[cfg(unix)]
#[test]
fn placement_rule_process_os_filter_reports_invalid_without_values() {
    use std::ffi::OsString;
    use std::os::unix::ffi::OsStringExt;
    let (process, diagnostics) = filter_process_env([
        (OsString::from(KEY), OsString::from("9")),
        (OsString::from("OTHER"), OsString::from_vec(vec![255])),
        (
            OsString::from_vec(b"PG_RULE_\xff".to_vec()),
            "secret".into(),
        ),
        (
            "PG_RULE_HOST_MEMORY_FLOOR_MIB".into(),
            OsString::from_vec(vec![255]),
        ),
    ]);
    assert_eq!(process, [(KEY.into(), "9".into())].into());
    assert_eq!(
        diagnostics,
        [
            "PG_RULE_<unknown> process invalid_key_utf8",
            "PG_RULE_<unknown> process invalid_value_utf8",
        ]
    );
    let dir = tempfile::tempdir().unwrap();
    let source = RuleSource {
        cache_path: dir.path().join("cache"),
        env_file: None,
        host_id: "fixture".into(),
        host_class: "".into(),
    };
    assert_eq!(
        source
            .load_with_env(&EmptyStore, &process)
            .unwrap()
            .report
            .values[KEY],
        9
    );
}

#[test]
fn placement_rule_documented_defaults_and_seeds_match_schema() {
    let docs = include_str!("../../../docs/placement-rules.md");
    let specs = schema().unwrap();
    assert_eq!(specs.len(), 2);
    let memory = specs.iter().find(|s| s.name == "HostMemoryFloor").unwrap();
    let worker = specs.iter().find(|s| s.name == "HostWorkerCap").unwrap();
    assert_eq!((memory.default, memory.alarm), (6144, Some(3000)));
    assert!(memory.host_seeds.is_empty());
    assert_eq!(worker.default, 6);
    assert_eq!(worker.alarm, None);
    for (key, spec, description) in [
        (
            "PG_RULE_HOST_MEMORY_FLOOR_MIB",
            memory,
            format!(
                "{}; alarm metadata {}",
                memory.default,
                memory.alarm.unwrap()
            ),
        ),
        (
            KEY,
            worker,
            format!(
                "{}; trusted host-class seeds {}",
                worker.default,
                worker
                    .host_seeds
                    .values()
                    .map(|seed| format!("a host → {seed}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        ),
    ] {
        assert!(docs.contains(&format!(
            "| `{key}` | {description} | {}–{} |",
            spec.min, spec.max
        )));
    }
}

#[test]
fn placement_rule_env_file_crlf_matches_lf_values_and_line_diagnostics() {
    let dir = tempfile::tempdir().unwrap();
    let source = RuleSource {
        cache_path: dir.path().join("cache"),
        env_file: Some(dir.path().join("env")),
        host_id: "fixture".into(),
        host_class: "".into(),
    };
    let mut reports = Vec::new();
    for newline in [b"\n".as_slice(), b"\r\n".as_slice()] {
        let lines: Vec<&[u8]> = vec![
            b"PG_RULE_HOST_WORKER_CAP_COUNT=9",
            b"broken",
            &[255],
            b"PG_RULE_HOST_MEMORY_FLOOR_MIB=7000",
            b"PG_RULE_HOST_WORKER_CAP_COUNT=8",
        ];
        let bytes: Vec<u8> = lines
            .into_iter()
            .flat_map(|line| line.iter().chain(newline).copied())
            .collect();
        fs::write(source.env_file.as_ref().unwrap(), bytes).unwrap();
        let loaded = source.load_with_env(&EmptyStore, &BTreeMap::new()).unwrap();
        assert_eq!(loaded.report.values[KEY], 9);
        assert_eq!(loaded.report.values["PG_RULE_HOST_MEMORY_FLOOR_MIB"], 7000);
        assert_eq!(
            loaded.report.diagnostics,
            [
                "env_file line 2 malformed",
                "env_file line 3 invalid_utf8",
                "env_file line 5 duplicate_key"
            ]
        );
        reports.push(loaded.report);
    }
    assert_eq!(reports[0].values, reports[1].values);
    assert_eq!(reports[0].diagnostics, reports[1].diagnostics);
}
