use super::*;

#[test]
fn inventory_attempt_identity_table() {
    let live = std::process::id();
    let stat = std::fs::read_to_string(format!("/proc/{live}/stat")).unwrap();
    let start: u64 = stat
        .rsplit_once(')')
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse()
        .unwrap();
    let proc = json!({"pid":live,"start_time":start,"pgid":2_000_000_000u32});
    for (name, attempts, count, expected) in [
        (
            "older null",
            json!([
            {"status":"running","started":1,"leg":{},"proc":null},
            {"status":"running","started":2,"leg":{},"proc":proc}]),
            2,
            None,
        ),
        (
            "last null refuses fallback",
            json!([
            {"status":"completed","proc":proc},
            {"status":"running","started":2,"leg":{},"proc":null}]),
            1,
            None,
        ),
        ("legacy fallback", json!([]), 1, Some(start)),
        (
            "sole first fallback",
            json!([
            {"status":"running","started":2,"leg":{},"proc":null}]),
            1,
            None,
        ),
        (
            "start mismatch",
            json!([
            {"status":"running","started":2,"leg":{},
             "proc":{"pid":live,"start_time":start+1,"pgid":2_000_000_000u32}}]),
            1,
            Some(start + 1),
        ),
        (
            "pid zero",
            json!([
            {"status":"running","started":2,"leg":{},"proc":{"pid":0}}]),
            1,
            None,
        ),
    ] {
        let dir = StateDir::new();
        let mut record = dir.record();
        record.set("proc", proc.clone());
        record.set("attempts", attempts);
        record.save().unwrap();
        let rows = inventory(&dir.0).unwrap();
        assert_eq!(rows.len(), count, "{name}");
        for (index, row) in rows.iter().enumerate() {
            let expected = if name == "older null" && index == 1 {
                Some(start)
            } else {
                expected
            };
            assert_eq!(
                row.identity.as_ref().and_then(|p| p.start_time),
                expected,
                "{name}"
            );
            match expected {
                None => assert!(matches!(
                    gone(&row.run_id, row.identity.as_ref()),
                    Err(ReconcileError::Unknown)
                )),
                Some(time) => assert_eq!(
                    gone(&row.run_id, row.identity.as_ref()).unwrap(),
                    time != start,
                    "{name}"
                ),
            }
        }
    }
}

#[test]
fn missing_attempt_start_has_zero_duration_and_no_deadline() {
    let dir = StateDir::new();
    let mut record = dir.record();
    record.set("max_s", json!(1));
    record.set(
        "attempts",
        json!([
        {"status":"running","leg":{"leg_key":"leg"}}]),
    );
    record.save().unwrap();
    assert!(inventory(&dir.0).unwrap()[0].deadline_ms.is_none());
    settle(&dir.0, &mut record, 1, "gone", 9000).unwrap();
    assert_eq!(
        record.get("attempts").unwrap()[0]["outcome"]["duration_s"],
        0
    );
}

#[test]
fn unknown_identity_is_bounded() {
    for deadline in [false, true] {
        let dir = StateDir::new();
        let mut record = dir.record();
        record.set(
            "attempts",
            json!([
            {"status":"completed"},
            {"status":"running","started":1,"max_s":if deadline { json!(1) } else { Value::Null },
             "leg":{"leg_key":"leg"},"proc":null}]),
        );
        record.save().unwrap();
        let row = inventory(&dir.0).unwrap().remove(0);
        if !deadline {
            assert!(matches!(
                orphan_gone(&dir.0, &row),
                Err(ReconcileError::Unknown)
            ));
            assert_eq!(
                RunRecord::read(&dir.0, &row.run_id).unwrap().get("status"),
                Some(&json!("running"))
            );
        }
        assert!(orphan_gone(&dir.0, &row).unwrap());
        let record = RunRecord::read(&dir.0, &row.run_id).unwrap();
        assert_eq!(record.get("status"), Some(&json!("orphaned")));
        assert_eq!(
            record.get("attempts").unwrap()[1]["reason"],
            "unknown_identity"
        );
        assert!(inventory(&dir.0).unwrap().is_empty());
    }
}
