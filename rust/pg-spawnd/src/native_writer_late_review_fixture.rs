use super::*;

pub(super) fn write_review_transport_fixture(
    cli: &Path,
    script: impl AsRef<str>,
) -> std::io::Result<()> {
    let route = r#"import json, sys
if sys.argv[sys.argv.index('--state') + 2] == 'route':
    print(json.dumps({'legs': [{'id': 'agy-gemini-4-hydrogen-high'}], 'skipped': []}))
    sys.exit(0)
"#;
    fs::write(cli, format!("{route}{}", script.as_ref()))
}

pub(super) fn check_review_resume_after_missing_client(
    host: &mut Host,
    base: &str,
    head: &str,
    cli: &Path,
) {
    let previous = json!({"head":base,"text":"HIGH src/lib.rs:1 - previous finding"});
    let diagnostic = "REVIEW run-missing FAIL client_missing /fixture/codex";
    write_review_transport_fixture(
        cli,
        format!("import json, sys\nprint(json.dumps(dict(run_id='run-missing', stderr_tail={diagnostic:?})))\nsys.exit(10)\n"),
    ).unwrap();
    let missing = host.review(head, Some(&previous)).unwrap();
    assert_eq!(missing["status"], "client_missing");
    assert_eq!(missing["reason"], diagnostic);

    write_review_transport_fixture(cli, r#"import json, pathlib, sys
argv = sys.argv
value = lambda flag: argv[argv.index(flag) + 1]
card = json.loads(value('--card'))
prompt = pathlib.Path(value('--prompt-file')).read_text()
assert 'HIGH src/lib.rs:1 - previous finding' in prompt, prompt
assert 'delta ' + card['diff_ref']['base'] + '..' + card['diff_ref']['head'] in prompt, prompt
print(json.dumps(dict(run_id='run-retry', status='completed', host='fixture-worker', head_sha=card['diff_ref']['head'], stdout_tail='VERDICT: REQUEST_CHANGES')))
"#).unwrap();
    let retried = host.review(head, Some(&previous)).unwrap();
    assert_eq!(retried["verdict"], "REQUEST_CHANGES", "{retried}");
}

pub(super) fn check_late_review_reuses_completed_run(host: &mut Host, head: &str, cli: &Path) {
    let run = host.state.join("runs/run-late");
    fs::create_dir_all(&run).unwrap();
    let record = run.join("record.json");
    let started = pg_worker_core::run::finalize::now().floor() as u64;
    let mut receipt = json!({"run_id":"run-late","status":"running","started":started});
    fs::write(&record, receipt.to_string()).unwrap();
    write_review_transport_fixture(
        cli,
        format!(
            "import sys\nassert sys.argv[sys.argv.index('--wait')+1] == '3600'\nprint({:?})\n",
            receipt.to_string()
        ),
    )
    .unwrap();
    let running = host.review(head, None).unwrap();
    assert_eq!(running["status"], "review_running");
    assert_eq!(running["run_id"], "run-late");
    assert_eq!(
        running["expected_end"].as_f64(),
        Some((started + 3600) as f64)
    );
    let pending = host.reviews.join(format!("late-review-{head}.json"));
    assert!(pending.is_file());
    assert!(!pending.with_extension("txt").exists());

    receipt["status"] = json!("completed");
    receipt["head_sha"] = json!(head);
    receipt["host"] = json!("fixture-worker");
    receipt["stdout_tail"] = json!("FILES READ: src/lib.rs\nVERDICT: ACCEPT\n");
    fs::write(&record, receipt.to_string()).unwrap();
    fs::remove_file(cli).unwrap();
    let reviewed = host.review(head, None).unwrap();
    assert_eq!(reviewed["run_id"], "run-late");
    assert_eq!(reviewed["verdict"], "ACCEPT");
    assert!(!pending.exists());
    let artifact = fs::read_to_string(pending.with_extension("txt")).unwrap();
    assert_eq!(
        artifact,
        format!("HEAD: {head}\nTESTS: none: reviewer supplied no test result\nFILES READ: src/lib.rs\nVERDICT: ACCEPT\n")
    );
}

pub(super) fn check_review_replaces_unreadable_run(host: &mut Host, head: &str, cli: &Path) {
    let pending = host.reviews.join(format!("late-review-{head}.json"));
    let run = host.state.join("runs/run-pruned/record.json");
    fs::create_dir_all(run.parent().unwrap()).unwrap();
    write_review_transport_fixture(cli, format!("import json\nprint(json.dumps(dict(run_id='run-replacement', status='completed', host='fixture-worker', head_sha={head:?}, stdout_tail='VERDICT: REQUEST_CHANGES')))\n")).unwrap();
    for corrupt in [false, true] {
        fs::write(&run, "corrupt record").unwrap();
        fs::write(
            &pending,
            json!({"head":head,"run_id":"run-pruned"}).to_string(),
        )
        .unwrap();
        if !corrupt {
            fs::remove_file(&run).unwrap();
        }
        let result = host.review(head, None).unwrap();
        assert_eq!(result["run_id"], "run-replacement");
        assert!(!pending.exists());
    }
}
