use super::*;
use std::io::{BufReader, Cursor};

const FIXTURE: &str = include_str!("../fixtures/probes.jsonl");

fn probes() -> Vec<Probe> {
    load(Cursor::new(FIXTURE)).unwrap()
}

fn config() -> RunConfig {
    RunConfig {
        leg: "offline-test".into(),
        fresh_session: true,
        tool_set: vec!["context".into(), "read".into()],
        repo_sha: "0123456789abcdef0123456789abcdef01234567".into(),
        no_memory_dir: true,
    }
}

fn evidence(probe: &Probe) -> (RunRecord, BTreeMap<String, SourceStatus>) {
    let run = RunRecord {
        final_answer: format!("The answer is {}.", probe.gold),
        cited_ids: vec!["D-653".into()],
        tool_calls: vec![ToolCall {
            tool: "context".into(),
            file_class: None,
        }],
    };
    let statuses = BTreeMap::from([("D-653".into(), SourceStatus::InForce)]);
    (run, statuses)
}

#[test]
fn loader_accepts_sample_from_temporary_directory() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("probes.jsonl");
    std::fs::write(&path, FIXTURE).unwrap();
    let loaded = load(BufReader::new(std::fs::File::open(path).unwrap())).unwrap();
    assert_eq!(loaded.iter().filter(|p| p.kind == Kind::Rule).count(), 3);
    assert_eq!(
        loaded.iter().filter(|p| p.kind == Kind::Knowledge).count(),
        2
    );
    assert!(loaded.iter().all(|p| p.planner_proposed));
}

#[test]
fn loader_rejects_duplicate_ids_with_line_number() {
    let row = FIXTURE.lines().next().unwrap();
    assert_eq!(
        load(Cursor::new(format!("{row}\n{row}\n"))),
        Err(LoadError::DuplicateId {
            line: 2,
            id: "rule-changelog".into()
        })
    );
}

#[test]
fn loader_rejects_missing_null_and_blank_gold() {
    let original = serde_json::to_value(&probes()[0]).unwrap();
    for gold in [
        None,
        Some(serde_json::Value::Null),
        Some(serde_json::json!("  ")),
    ] {
        let mut row = original.clone();
        row.as_object_mut().unwrap().remove("gold");
        if let Some(value) = gold {
            row["gold"] = value;
        }
        assert_eq!(
            load(Cursor::new(row.to_string())),
            Err(LoadError::MissingGold { line: 1 })
        );
    }
}

#[test]
fn loader_rejects_unknown_kind_and_malformed_json() {
    let row = FIXTURE.replacen("\"rule\"", "\"mystery\"", 1);
    assert_eq!(
        load(Cursor::new(row)),
        Err(LoadError::UnknownKind {
            line: 1,
            kind: "mystery".into()
        })
    );
    assert_eq!(
        load(Cursor::new("{")),
        Err(LoadError::InvalidJson { line: 1 })
    );
}

#[test]
fn correct_current_citation_gets_full_marks() {
    let probe = &probes()[0];
    let (run, statuses) = evidence(probe);
    let result = score(probe, &run, &statuses, &config());
    assert_eq!(result.answer_accuracy, 1.);
    assert_eq!(result.source_currency, 1.);
    assert_eq!(result.retrieval_path, 1.);
    assert!(!result.reason.is_empty());
}

#[test]
fn currency_fails_closed_for_any_stale_unknown_or_absent_citation() {
    let probe = &probes()[0];
    let (mut run, mut statuses) = evidence(probe);
    run.cited_ids.push("D-old".into());
    for status in [SourceStatus::Superseded, SourceStatus::History] {
        statuses.insert("D-old".into(), status);
        assert_eq!(score(probe, &run, &statuses, &config()).source_currency, 0.);
    }
    statuses.remove("D-old");
    assert_eq!(score(probe, &run, &statuses, &config()).source_currency, 0.);
    run.cited_ids.clear();
    assert_eq!(score(probe, &run, &statuses, &config()).source_currency, 0.);
}

#[test]
fn right_answer_wrong_path_is_only_partial_credit() {
    let probe = &probes()[0];
    let (mut run, statuses) = evidence(probe);
    run.tool_calls[0].tool = "context-history".into();
    let result = score(probe, &run, &statuses, &config());
    assert_eq!(result.answer_accuracy, 1.);
    assert_eq!(result.source_currency, 1.);
    assert_eq!(result.retrieval_path, 0.);
    assert!(result.reason.contains("path_mismatch"));
}

#[test]
fn knowledge_answer_and_file_class_match_recorded_evidence() {
    let probe = &probes()[3];
    let (mut run, statuses) = evidence(probe);
    run.final_answer = format!("{}!", probe.gold.to_uppercase().replace(' ', "  "));
    run.tool_calls = vec![ToolCall {
        tool: "read".into(),
        file_class: Some("repository_source".into()),
    }];
    assert_eq!(score(probe, &run, &statuses, &config()).retrieval_path, 1.);
    assert_eq!(score(probe, &run, &statuses, &config()).answer_accuracy, 1.);
    run.tool_calls.clear();
    run.final_answer = "Wrong fact".into();
    let result = score(probe, &run, &statuses, &config());
    assert_eq!((result.answer_accuracy, result.retrieval_path), (0., 0.));
}

#[test]
fn answer_requires_gold_tokens_not_a_citation_or_id_prefix() {
    let probe = &probes()[0];
    let (mut run, statuses) = evidence(probe);
    run.final_answer = "D-6530".into();
    assert_eq!(score(probe, &run, &statuses, &config()).answer_accuracy, 0.);
}

#[test]
fn scores_are_deterministic_and_serialize_with_the_entire_config() {
    let probe = &probes()[0];
    let (run, statuses) = evidence(probe);
    let result = score(probe, &run, &statuses, &config());
    let json = serde_json::to_string(&result).unwrap();
    assert_eq!(
        json,
        serde_json::to_string(&score(probe, &run, &statuses, &config())).unwrap()
    );
    assert_eq!(serde_json::from_str::<Score>(&json).unwrap(), result);
    let value = serde_json::to_value(&result).unwrap();
    assert_eq!(value["config"], serde_json::to_value(config()).unwrap());
    assert_eq!(value["probe_id"], probe.id);
}
