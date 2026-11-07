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
    for (class, seed) in [("dispatch_vm", 4), ("worker-a", 6)] {
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
        host_class: "worker-a".into(),
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
