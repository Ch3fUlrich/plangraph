use super::*;

#[test]
fn portable_identity_leader_state_table() {
    let mut counts = [0, 0];
    // X is not reliably constructible on Linux; inject its observable start-time
    // result. Non-Linux cannot verify leader start times, so all rows need grace.
    for state in ["live", "Z", "X", "gone"] {
        for pid in [0, 10, 20] {
            for pgid in [None, Some(10), Some(20)] {
                for grouped in [false, true] {
                    for start_time in [None, Some(100), Some(101)] {
                        let row = Entry {
                            run_id: "run-test".into(),
                            role: "implement".into(),
                            attempt: 1,
                            deadline_ms: None,
                            identity: Some(Proc {
                                pid,
                                pgid,
                                start_time,
                                group: grouped.then(|| "scope".into()),
                            }),
                        };
                        let matching = state == "live"
                            && start_time == Some(100)
                            && (pid == 10 || (pid == 0 && pgid == Some(10)));
                        let grace = needs_grace_with(&row, |id| {
                            (state == "live" && id == 10).then_some(100)
                        });
                        assert_eq!(grace, !matching);
                        counts[usize::from(!grace)] += 1;
                        #[cfg(not(target_os = "linux"))]
                        assert!(needs_grace(&row));
                    }
                }
            }
        }
    }
    assert_eq!(counts, [208, 8]);
}
