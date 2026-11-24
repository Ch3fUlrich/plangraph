use super::{
    build::build,
    build_tests::{part, Fx},
};
use crate::router_batch::{go_row, GoInput, Part, Seat};
use pg_worker_core::counted_seat::{Binding, Revision, Selection};
use serde_json::{json, Value};
use std::{collections::BTreeSet, fs, path::Path, process::Command};

const OLD: &str = "opencode-go-deepseek-v4-flash";
const NEW: &str = "opencode-go-glm-5.3-flash";
const KEY: &str = "fixture:PART";
const CHILD: &str = "PG_TEST_COUNTED_LANDING";
const TEST: &str = "land::counted_receipt_tests::configured_landing_gates_follow_the_fourth_lane";

fn bind(head: &str, lane: &str, candidate: bool, run: &str) -> Binding {
    Binding {
        selection: Selection {
            seat: Revision {
                revision: if candidate { "new" } else { "old" }.into(),
                leg: if candidate { NEW } else { OLD }.into(),
            },
            lane: Some(lane.into()),
        },
        head: head.into(),
        run_id: run.into(),
        item_key: Some(KEY.into()),
        diff_lines: Some(130),
        writer_families: BTreeSet::from(["openai".into()]),
    }
}

fn save(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

#[test]
fn configured_landing_gates_follow_the_fourth_lane() {
    // Both entry points load the actual configured policy. Isolate their
    // policy and mirror configuration from the rest of the test process.
    if let Some(root) = std::env::var_os(CHILD) {
        check_landing(Path::new(&root));
        return;
    }
    let root = tempfile::tempdir().unwrap();
    let python = root.path().join("mirror");
    pg_worker_core::executable_test_support::write_private_executable(
        &python,
        "#!/bin/sh\n[ \"$1\" = '-I' ] && [ \"$2\" = '-c' ] && [ \"$4\" = 'fixture:PART' ] || exit 1\nexec cat \"$PG_TEST_COUNTED_LANDING/$6.json\"\n",
    );
    let result = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", TEST, "--nocapture"])
        .env(CHILD, root.path())
        .env("PG_SPAWND_PYTHON", python)
        .env("PLANGRAPH_ROUTE_POLICY", root.path().join("route.json"))
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}

fn check_landing(root: &Path) {
    let fx = Fx::new();
    let head = fx.commit(&fx.base(), "part.txt", "fourth lane\n");
    let old = bind(&head, "lane-d", false, "run-old");
    let new = bind(&head, "lane-d", true, "run-new");
    for binding in [&old, &new] {
        save(
            &root.join(format!("{}.json", binding.run_id)),
            &json!({"leg":binding.selection.seat.leg,"counted_review":binding}),
        );
    }
    let mut rollout = json!({
        "previous":old.selection.seat, "candidate":new.selection.seat,
        "cohort":["lane-a","lane-b","lane-c"], "heads":{(&head):"lane-d"}
    });
    let mut samples = Vec::new();
    for i in 0..20 {
        let sample_head = format!("{i:040x}");
        let candidate = i >= 10;
        let lane = if candidate {
            ["lane-a", "lane-b", "lane-c"][i % 3]
        } else {
            "lane-d"
        };
        rollout["heads"][&sample_head] = json!(lane);
        samples.push(json!({
            "binding":bind(&sample_head,lane,candidate,&format!("run-sample{i}")),
            "accepted":true,"round":1
        }));
    }

    let go = |binding: &Binding, key: Option<&str>, run: Option<&str>| {
        let input = GoInput {
            decision: "D-6400".into(),
            project: "fixture".into(),
            pr: 80,
            head: head.clone(),
            parts: vec![Part {
                name: "fourth".into(),
                head: head.clone(),
                ci: "green".into(),
                writer_family: "openai".into(),
                item_key: key.map(str::to_owned),
                seats: vec![Seat {
                    leg: binding.selection.seat.leg.clone(),
                    review_file: "review.txt".into(),
                    reviewer_family: if binding.selection.seat.leg == OLD {
                        "deepseek"
                    } else {
                        "glm"
                    }
                    .into(),
                    run_id: run.map(str::to_owned),
                }],
            }],
        };
        go_row(
            &input,
            &|_| {
                Ok(format!(
                    "FILES READ: part.txt\nHEAD: {head}\nVERDICT: ACCEPT\n"
                ))
            },
            &str::to_owned,
        )
    };
    let train = |binding: &Binding, run: &str| {
        let mut fourth = part(4, &head, None);
        fourth.receipt.run_id = run.into();
        fourth.receipt.counted_review = Some(binding.clone());
        build(&fx.input(vec![fourth], "", None)).map(|out| out.result.merged.len())
    };

    for (decision, allowed, refused) in [
        (Value::Null, &old, &new),
        (
            json!({"action":"promote","revision":"new","samples":samples}),
            &new,
            &old,
        ),
        (json!({"action":"rollback","revision":"new"}), &old, &new),
    ] {
        let mut document = json!({"counted_review":rollout});
        document["counted_review"]["decision"] = decision;
        save(&root.join("route.json"), &document);
        assert!(go(allowed, Some(KEY), Some(&allowed.run_id)).is_ok());
        assert_eq!(train(allowed, &allowed.run_id), Ok(1));
        assert!(go(refused, Some(KEY), Some(&refused.run_id))
            .unwrap_err()
            .contains("counted review seat is missing"));
        assert!(train(refused, &refused.run_id)
            .unwrap_err()
            .contains("lane, revision"));
        assert!(go(allowed, Some(KEY), Some(&refused.run_id))
            .unwrap_err()
            .contains("lane, revision"));
        assert!(go(allowed, None, Some(&allowed.run_id))
            .unwrap_err()
            .contains("part item key"));
        assert!(go(allowed, Some(KEY), None)
            .unwrap_err()
            .contains("running seat ID"));
        assert!(train(allowed, "")
            .unwrap_err()
            .contains("run ID is invalid"));
        let mut missing_item = allowed.clone();
        missing_item.item_key = None;
        assert_eq!(
            train(&missing_item, &allowed.run_id).unwrap_err(),
            "missing counted review item"
        );
    }
}
