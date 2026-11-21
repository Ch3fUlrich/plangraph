use super::*;
use serde_json::json;

#[test]
fn append_during_acknowledgement_survives_reopening() {
    let dir = tempfile::tempdir().unwrap();
    let legacy = dir.path().join("outbox.jsonl");
    let queue = Queue::open_bound(&legacy, "owner").unwrap();
    let first = queue
        .enqueue("heartbeat", json!({"key":"d:A","epoch":1}))
        .unwrap();
    let recovered = Queue::open_bound(&legacy, "owner").unwrap();
    let mut seen = Vec::new();
    recovered
        .drain(
            None,
            |_| Ok(None),
            |entry| {
                seen.push(entry.id.clone());
                if entry.id == first {
                    queue
                        .enqueue("heartbeat", json!({"key":"d:A","epoch":1}))
                        .unwrap();
                }
                Ok(json!({"id":entry.id,"result":"ok","reply":{}}))
            },
        )
        .unwrap();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0], first);
    assert_ne!(seen[0], seen[1]);
    assert!(recovered
        .drain(None, |_| Ok(None), |_| panic!("already acknowledged"))
        .unwrap()
        .is_empty());
}

#[test]
fn live_submit_cannot_pass_unverified_evidence() {
    let dir = tempfile::tempdir().unwrap();
    let queue = Queue::open(&dir.path().join("outbox.jsonl")).unwrap();
    queue
        .enqueue("add_evidence", json!({"key":"d:A","epoch":1}))
        .unwrap();
    let submit = queue
        .enqueue("submit", json!({"key":"d:A","epoch":1,"source_round":0}))
        .unwrap();
    let result = queue.drain(
        Some(&submit),
        |_| Ok(None),
        |entry| {
            assert_eq!(entry.verb, "add_evidence");
            Ok(json!({"id":entry.id,"result":"ok","reply":{"verified":false}}))
        },
    );
    assert!(result.unwrap_err().to_string().contains("evidence"));
}

#[test]
fn legacy_import_keeps_ids_and_order_without_replaying_unproven_entries() {
    let dir = tempfile::tempdir().unwrap();
    let legacy = dir.path().join("outbox.jsonl");
    let input = [
        json!({"id":"a".repeat(32),"verb":"heartbeat","body":{}}),
        json!({"id":"b".repeat(32),"verb":"add_evidence","body":{}}),
    ]
    .iter()
    .map(|v| format!("{v}\n"))
    .collect::<String>();
    fs::write(&legacy, &input).unwrap();
    let queue = Queue::open_bound(&legacy, "owner").unwrap();
    let live = queue.enqueue("heartbeat", json!({})).unwrap();
    let mut seen = Vec::new();
    assert!(queue
        .drain(
            None,
            |_| Ok(None),
            |entry| {
                seen.push(entry.id.clone());
                assert!(entry.legacy);
                let result = if entry.id == "a".repeat(32) {
                    "duplicate"
                } else {
                    "recovery_required"
                };
                Ok(json!({"id":entry.id,"result":result,"legacy":true,"first":"ok"}))
            }
        )
        .unwrap_err()
        .to_string()
        .contains("requires recovery"));
    assert_eq!(seen, ["a".repeat(32), "b".repeat(32)]);
    let recovered = Queue::open_bound(&legacy, "owner").unwrap();
    recovered.resolve(&"b".repeat(32), "D-1").unwrap();
    let outcomes = recovered
        .drain(
            None,
            |_| Ok(None),
            |entry| {
                assert!(!entry.legacy);
                assert_eq!(entry.id, live);
                Ok(json!({"id":entry.id,"result":"ok","reply":{}}))
            },
        )
        .unwrap();
    assert_eq!(outcomes.len(), 1);
    assert_eq!(fs::read_to_string(legacy).unwrap(), input);
}

#[cfg(unix)]
#[test]
fn symlinked_legacy_queue_does_not_touch_target() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join("target");
    fs::write(&target, "unchanged").unwrap();
    let legacy = dir.path().join("outbox.jsonl");
    std::os::unix::fs::symlink(&target, &legacy).unwrap();
    assert!(Queue::open_bound(&legacy, "owner").is_err());
    assert_eq!(fs::read_to_string(target).unwrap(), "unchanged");
}

#[test]
fn bound_submit_survives_restart_after_send() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("outbox.jsonl");
    let queue = Queue::open_bound(&path, "owner").unwrap();
    let id = queue
        .enqueue("submit", json!({"key":"d:A","epoch":1}))
        .unwrap();
    let bound = json!({"key":"d:A","epoch":1,"source_round":2});
    assert!(queue
        .drain(
            Some(&id),
            |_| Ok(Some(bound.clone())),
            |entry| {
                assert_eq!(entry.body, bound);
                Err(io::Error::other("reply lost"))
            }
        )
        .is_err());
    let recovered = Queue::open_bound(&path, "owner").unwrap();
    let result = recovered
        .drain(
            Some(&id),
            |entry| {
                assert_eq!(entry.body, bound);
                Ok(None)
            },
            |entry| {
                assert_eq!(entry.id, id);
                assert_eq!(entry.body, bound);
                Ok(json!({"id":entry.id,"result":"duplicate","first":"ok"}))
            },
        )
        .unwrap();
    assert_eq!(result[0]["result"], "duplicate");
}

#[test]
fn foreign_credential_cannot_import_into_existing_queue() {
    let dir = tempfile::tempdir().unwrap();
    let legacy = dir.path().join("outbox.jsonl");
    let owner = Queue::open_bound(&legacy, "owner").unwrap();
    fs::write(
        &legacy,
        format!(
            "{}\n",
            json!({
                "id": "a".repeat(32), "verb": "heartbeat", "body": {}
            })
        ),
    )
    .unwrap();
    assert!(Queue::open_bound(&legacy, "foreign").is_err());
    assert!(owner.first_pending().unwrap().is_none());
}
