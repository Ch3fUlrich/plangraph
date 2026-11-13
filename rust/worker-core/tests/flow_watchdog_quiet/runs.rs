use super::*;

#[test]
fn unreadable_run_records_never_count_as_ended_or_an_empty_list() {
    let cases = json!([null, {"error":"unreadable"}, [null], [{"run_id":"broken","ended":9000}],
        [{"run_id":"broken","parent_session":"","ended":9000}],
        [{"run_id":"broken","parent_session":null,"ended":9000}],
        [{"run_id":"broken","parent_session":17,"ended":9000}]]);
    for runs in cases.as_array().unwrap() {
        let diagnostic = if runs.is_array() {
            " run_record_index=0"
        } else {
            ""
        };
        let mut s = snapshot();
        add_session(&mut s, "session-2");
        s["session_activity"]["runs"] = runs.clone();
        let found = events(&s);
        assert_eq!(found.len(), 2, "{found:?}");
        for (event, id) in found.iter().zip(["session-1", "session-2"]) {
            let detail = format!("session={id:?} role=\"build\" unknown=runs{diagnostic}");
            exact(std::slice::from_ref(event), &detail);
        }
    }
}

#[test]
fn malformed_runs_only_invalidate_their_known_parent() {
    let cases = json!([{"ended":9000}, {"run_id":"","ended":9000}, {"run_id":17,"ended":9000},
        {"run_id":"broken"}, {"run_id":"broken","ended":"bad"},
        {"run_id":"broken","ended":NOW + 1}, {"run_id":"broken","ended":-1},
        {"run_id":"broken","ended":true}]);
    for record in cases.as_array().unwrap() {
        for (parent, other) in [("session-1", "session-2"), ("session-2", "session-1")] {
            let mut s = snapshot();
            add_session(&mut s, "session-2");
            s["session_activity"]["runs"][2]["ended"] = 9500.into();
            let mut record = record.clone();
            record["parent_session"] = parent.into();
            rows(&mut s, "runs").push(record);
            let found = events(&s);
            assert_eq!(found.len(), 2, "{found:?}");
            let affected = found
                .iter()
                .find(|e| message(e).contains(&format!("session={parent:?} ")))
                .unwrap();
            let reason = "unknown=runs run_record_index=3";
            assert!(message(affected).ends_with(reason), "{found:?}");
            assert!(found
                .iter()
                .any(|e| message(e).contains("quiet_age_seconds=1260 ")));
            rows(&mut s, "runs").last_mut().unwrap()["ended"] = Value::Null;
            let live = events(&s);
            assert_eq!(live.len(), 1, "{live:?}");
            assert!(live
                .iter()
                .all(|event| !message(event).contains(&format!("session={parent:?} "))));
            let detail = format!("session={other:?} role=\"build\" quiet_age_seconds=1260 ");
            assert!(line(&live).contains(&detail));
        }
    }
}

#[test]
fn missing_or_unreadable_run_block_is_unknown_even_without_busy_sessions() {
    let cases = json!([null, {"error":"unreadable"}, [{"run_id":"broken"}],
        [{"parent_session":"session-1","ended":"bad"}]]);
    for runs in std::iter::once(None).chain(cases.as_array().unwrap().iter().map(Some)) {
        let indexed = runs.is_some_and(Value::is_array);
        let diagnostic = if indexed { " run_record_index=0" } else { "" };
        for sessions in [json!([]), json!([{"busy":false}])] {
            let mut s = snapshot();
            s["session_activity"]["sessions"] = sessions;
            let activity = &mut s["session_activity"];
            if let Some(runs) = runs {
                activity["runs"] = runs.clone();
            } else {
                activity.as_object_mut().unwrap().remove("runs");
            }
            exact(&events(&s), &format!("unknown=runs{diagnostic}"));
            if indexed {
                let dir = tempfile::tempdir().unwrap();
                let first = pass(dir.path(), &s);
                let valid = json!({"run_id":"other","parent_session":"other","ended":9000});
                rows(&mut s, "runs").insert(0, valid);
                assert_eq!(pass(dir.path(), &s), first);
                assert_eq!(first[0]["active"], "busy-quiet unknown=runs");
                assert_eq!(active(dir.path()), "busy-quiet unknown=runs");
                exact(&events(&s), "unknown=runs run_record_index=1");
            }
        }
    }
}
