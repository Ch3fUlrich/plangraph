use super::*;

#[cfg(unix)]
#[test]
fn native_writer_binds_replay_evidence_to_review_card() {
    let dir = tempfile::tempdir().unwrap();
    let git = |cwd: &Path, args: &[&str]| {
        output(
            Command::new("git")
                .arg("-C")
                .arg(cwd)
                .args(args)
                .env("GIT_AUTHOR_NAME", "fixture")
                .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
                .env("GIT_COMMITTER_NAME", "fixture")
                .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
        )
        .unwrap()
    };
    let remote = dir.path().join("remote.git");
    let cwd = dir.path().join("worktree");
    fs::create_dir_all(&cwd).unwrap();
    git(
        dir.path(),
        &["init", "-q", "--bare", remote.to_str().unwrap()],
    );
    git(&cwd, &["init", "-q", "-b", "main"]);
    git(&cwd, &["remote", "add", "origin", remote.to_str().unwrap()]);
    // The commit-trailer gate records its arguments and fails while gate-red exists.
    fs::create_dir_all(cwd.join("tools")).unwrap();
    fs::write(
        cwd.join("tools/ci_commit_gate.py"),
        "import pathlib, subprocess, sys\nhead = subprocess.check_output(['git', 'rev-parse', sys.argv[sys.argv.index('--head') + 1]], text=True).strip()\npathlib.Path('../gate-runs.txt').open('a').write(' '.join(sys.argv[1:]) + ' ' + head + '\\n')\nsys.exit(1 if pathlib.Path('../gate-red').exists() else 0)\n",
    )
    .unwrap();
    fs::write(cwd.join("behavior.py"), "answer = 1\n").unwrap();
    git(&cwd, &["add", "tools", "behavior.py"]);
    git(&cwd, &["commit", "-q", "-m", "base"]);
    let base = git(&cwd, &["rev-parse", "HEAD"]);
    git(
        &cwd,
        &["update-ref", "refs/plangraph/native/run-host/base", &base],
    );
    fs::create_dir_all(cwd.join("src")).unwrap();
    fs::write(
        cwd.join("src/lib.rs"),
        "// fixture_symbol is documented here\nfn caller() {\n    fixture_symbol();\n}\n",
    )
    .unwrap();
    git(&cwd, &["add", "src/lib.rs"]);
    git(
        &cwd,
        &[
            "commit",
            "-q",
            "-m",
            "writer\n\nSKIPPED plangraph:OTHER-HOST-ITEM Fixture contract unavailable.\nSKIPPED plangraph:UNLISTED-ITEM Not in this brief.\nSKIPPED plangraph:HOST-TEST-ITEM Waiting for fixture.\n\nWriter: gemini-4-hydrogen-high run-6f00f4b3",
        ],
    );
    fs::write(cwd.join("src/more.rs"), "pub fn fixture_symbol() {}\n").unwrap();
    fs::write(cwd.join("behavior.py"), "answer = 2\n").unwrap();
    fs::create_dir_all(cwd.join("tests")).unwrap();
    fs::write(
        cwd.join("tests/test_replay.py"),
        "from behavior import answer\n\ndef test_change():\n    assert answer == 2\n",
    )
    .unwrap();
    git(&cwd, &["add", "src/more.rs", "behavior.py", "tests"]);
    git(
        &cwd,
        &[
            "commit",
            "-q",
            "-m",
            "claude\n\nPlan-Item: plangraph:HOST-TEST-ITEM\nWriter: claude-opus-5-5 session_abc",
        ],
    );
    let head = git(&cwd, &["rev-parse", "HEAD"]);
    // The review skill of the checkout (the working-tree copy: this fixture has no origin/main).
    // (Joined from parts: a fixture file, not a repository input of the CI selector.)
    let skill_dir = cwd.join("skills").join("review");
    fs::create_dir_all(&skill_dir).unwrap();
    let skill = include_str!("../../../skills/review/SKILL.md");
    let extended_skill = format!("{skill}\nRun a shell command to check the fixture.\n");
    fs::write(skill_dir.join("SKILL.md"), &extended_skill).unwrap();
    // The writer's checkout carries a planted family table: it must never be imported.
    // (File names are assembled: they are fixtures, not Python inputs of the CI selector.)
    let py = |stem: &str| format!("{stem}.{}", "py");
    let planted = cwd.join("src/plangraph");
    fs::create_dir_all(planted.join("review")).unwrap();
    fs::write(planted.join(py("__init__")), "").unwrap();
    fs::write(planted.join("review").join(py("__init__")), "").unwrap();
    fs::write(
        planted.join("review").join(py("family")),
        "def model_family(model):\n    return 'planted'\n",
    )
    .unwrap();
    let state = dir.path().join("state");
    fs::create_dir_all(state.join("runs/run-host")).unwrap();
    let cli = dir.path().join("review-cli.py");
    fs::write(
        &cli,
        r#"import json, pathlib, subprocess, sys
sys.path.insert(0, sys.argv[1])
from plangraph.spawner.prompt_input import compose, resolve_prompt
argv = sys.argv[2:]
value = lambda flag: argv[argv.index(flag) + 1]
state, card = pathlib.Path(value('--state')), json.loads(value('--card'))
assert value('--role') == 'review' and card['repository_review_required']
verb = argv[argv.index('--state') + 2]
if verb == 'route':
    if (state / 'no-review-seat').exists():
        print(json.dumps({'legs': [], 'skipped': [
            {'leg': 'agy-gemini-4-hydrogen-high', 'reason': 'limited'},
            {'leg': 'mimo-v2.6-pro', 'reason': 'repository_review_tools_unavailable'}]}))
    else:
        print(json.dumps({'legs': [{'id':'agy-gemini-4-hydrogen-high'}], 'skipped': []}))
    sys.exit(0)
assert verb == 'spawn' and '--cwd' not in argv
families = [argv[i + 1] for i, a in enumerate(argv) if a == '--exclude-family']
# The Writer trailers of base..head (google, anthropic) and the served writer model (openai).
assert sorted(families) == ['anthropic', 'google', 'openai'], families
assert 'cwd' not in card
prompt = resolve_prompt(None, value('--prompt-file'))
assert prompt.startswith('[tools-only]'), prompt
inline = compose(prompt, 'review', 'opencode', None, tool_mode='inline-only')
assert 'Run a shell command to check the fixture.' not in inline, inline
assert 'Item key: plangraph:HOST-TEST-ITEM' in inline, inline
assert 'Done criteria: By test: first line\nsecond criteria line\n' in inline, inline
assert 'Invariants named by the item: never push\n\nWriter brief (item keys and Done-when lines):\nkeep this quoted heading' in inline, inline
head = card['diff_ref']['head']
repo = pathlib.Path(card['diff_ref']['repo'])
assert repo == pathlib.Path.cwd()
assert card['review_workspace'] is True
receipt = card['test_replay']
assert receipt['base'] == card['diff_ref']['base'] and receipt['head'] == head, receipt
assert 'tests/test_replay.py::test_change' in inline, inline
assert 'base=failed' in inline and 'head=passed' in inline, inline
assert subprocess.check_output(['git', '-C', str(repo), 'rev-parse', head], text=True).strip() == head
assert 'Writer brief' in prompt and 'the item brief' in prompt and head in prompt, prompt
assert 'SKIPPED plangraph:OTHER-HOST-ITEM Fixture contract unavailable.\n' in prompt, prompt
assert 'SKIPPED plangraph:UNLISTED-ITEM' not in prompt, prompt
assert 'SKIPPED plangraph:HOST-TEST-ITEM' not in prompt, prompt
assert 'excluded from this review): anthropic, google, openai.' in prompt, prompt
body = prompt.split('[/review-skill]\n\n', 1)[1]
assert inline.split('\n\n', 1)[1] == body, inline
assert 'git diff ' not in inline and 'Open callers' not in inline and 'CARGO_TARGET_DIR' not in inline, inline
assert body.startswith('Role: independent reviewer') and '=== ITEM CONTEXT' in prompt, prompt
skill, context = prompt.index('Run a shell command to check the fixture.'), prompt.index('=== ITEM CONTEXT')
assert skill < context, prompt
tools = compose(prompt, 'review', 'opencode', None, tool_mode='tools')
assert tools.split('\n\n', 1)[1] == prompt, tools
assert compose(tools, 'review', 'opencode', None, tool_mode='tools') == tools
assert prompt.index('Open callers') < prompt.index('[/tools-only]') < context, prompt
assert 'TEST-GAMING:' in body.split('=== ITEM CONTEXT', 1)[0], prompt
assert 'Item key: plangraph:HOST-TEST-ITEM' in prompt, prompt
assert 'Done criteria: By test: first line\nsecond criteria line\n' in prompt, prompt  # multi-line, whole
assert 'Invariants named by the item: never push\n' in prompt, prompt
assert 'Exact base SHA: ' + card['diff_ref']['base'] + '\n' in prompt, prompt
assert 'Exact head SHA: ' + head + '\n' in prompt, prompt
assert 'Changed public symbols: fixture_symbol\n' in prompt, prompt
assert 'fixture_symbol: src/lib.rs:3\n' in prompt, prompt  # the code use, not the comment or the definition
import re
log = re.search(r'fast checks \(exact head\): ([^;\s]+)', prompt).group(1)
assert log.endswith('/fast-checks/' + head + '.log') and '== ci_commit_gate.py' in pathlib.Path(log).read_text(), prompt
excerpt = re.search(r'with their cargo runs: (\S+\.session\.log)', prompt).group(1)
assert excerpt.endswith('/fast-checks/' + head + '.session.log') and 'cargo test: 3 passed' in pathlib.Path(excerpt).read_text(), prompt
assert '== ci_commit_gate.py (exit status: 0)' in pathlib.Path(log).read_text(), prompt
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == head
run = state / 'runs' / 'run-rv1'
run.mkdir(parents=True, exist_ok=True)
text = 'FILES READ: src/lib.rs:1\nHEAD: ' + head + '\nVERDICT: ACCEPT\n'
(run / 'live.log').write_text(json.dumps({'type': 'result', 'is_error': False, 'result': text}) + '\n')
print('banner line')
print(json.dumps({'run_id': 'run-rv1', 'status': 'completed', 'host': 'fixture-worker', 'head_sha': head, 'live_log': str(run / 'live.log'), 'chosen_leg': {'id': 'agy-gemini-4-hydrogen-high', 'harness': 'agy', 'tool_mode': 'inline-only' if (state / 'inline-seat').exists() else 'tools'}}, indent=2))
"#,
    )
    .unwrap();
    let mut host = Host {
        repo: cwd.clone(),
        run_id: "run-host".into(),
        cwd: cwd.clone(),
        reviews: dir.path().join("reviews"),
        record_path: state.join("runs/run-host/record.json"),
        base_ref: "refs/heads/main".into(),
        tool_path: std::env::var("PATH").unwrap(),
        effort: WRITER_EFFORT.into(),
        state: state.clone(),
        brief: "the item brief\nPlan item: plangraph:HOST-TEST-ITEM\nTitle: t\n\nInvariants:\nnever push\n\nWriter brief (item keys and Done-when lines):\nkeep this quoted heading\n\nDone criteria:\nBy test: first line\nsecond criteria line\n\nFiles:\nsrc/lib.rs\n\nPlan item: plangraph:OTHER-HOST-ITEM\n"
            .into(),
        push_ref: None,
        reviews_started: 0,
        review_override: None,
        review_cli: vec![
            "python3".into(),
            cli.to_string_lossy().into_owned(),
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../src")
                .to_string_lossy()
                .into_owned(),
        ],
        python_src: Some(Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")),
    };
    assert!(!host.stamped(&head));
    // A red commit-trailer gate fails the fast checks without a stamp and blocks the final gates.
    fs::write(dir.path().join("gate-red"), "").unwrap();
    assert!(host
        .fast_checks(&head)
        .unwrap_err()
        .starts_with("ci_commit_gate.py"));
    assert!(!host.stamped(&head));
    assert!(host.gates().unwrap_err().starts_with("ci_commit_gate.py"));
    fs::remove_file(dir.path().join("gate-red")).unwrap();
    // The writer session log of the turns that produced this head (cargo runs included).
    fs::write(
        state.join("runs/run-host/native.log"),
        "cargo test: 3 passed\n",
    )
    .unwrap();
    fs::write(cwd.join("behavior.py"), "answer = 3\n").unwrap();
    host.fast_checks(&head).unwrap();
    fs::write(cwd.join("behavior.py"), "answer = 2\n").unwrap();
    assert!(host.stamped(&head));
    assert!(!host.stamped(&base));
    host.gates().unwrap();
    let runs = fs::read_to_string(dir.path().join("gate-runs.txt")).unwrap();
    let expected = format!("check --base refs/plangraph/native/run-host/base --head HEAD {head}");
    assert_eq!(runs.lines().filter(|l| *l == expected).count(), 4, "{runs}");
    super::super::review_tests::save_writer_without_artifacts(&mut host);
    let review = host.review(&head, None).unwrap();
    assert_eq!(review["verdict"], "ACCEPT", "{review}");
    assert_eq!(review["head"], head.as_str());
    assert_eq!(review["run_id"], "run-rv1");
    assert!(super::super::review_tests::saved_prompt(&host, &head, 1)
        .contains("\nwriter status: missing\n"));
    assert_eq!(review["leg"], "agy-gemini-4-hydrogen-high");
    assert_eq!(review["harness"], "agy");
    assert_eq!(review["host"], "fixture-worker");
    assert!(Path::new(review["path"].as_str().unwrap()).is_file());
    assert!(!host
        .reviews
        .join(format!("checkout-{}-1", &head[..8]))
        .exists());
    let unfamiliar_skill =
        extended_skill.replace("name: review", "name: review\nversion: unfamiliar");
    fs::write(skill_dir.join("SKILL.md"), unfamiliar_skill).unwrap();
    super::super::review_tests::write_writer_artifacts(&host);
    let review = host.review(&head, None).unwrap();
    assert_eq!(review["verdict"], "ACCEPT", "{review}");
    super::super::review_tests::assert_writer_artifacts(&host, &head);
    // OpenCode legs: no result frame in the live log, the answer is the stdout tail.
    let record = json!({"run_id":"run-rv2", "status":"completed",
        "stdout_tail":format!("HIGH src/lib.rs:1 - x\nHEAD: {head}\nVERDICT: REQUEST_CHANGES\n")});
    let text = review_run_text(&state, &record).unwrap();
    assert_eq!(
        review_verdict(&text, &head, &[])["verdict"],
        "REQUEST_CHANGES"
    );
    fs::write(state.join("inline-seat"), "").unwrap();
    let inline = host.review(&head, None).unwrap();
    assert_eq!(inline["verdict"], "no_verdict", "{inline}");
    assert_eq!(inline["reason"], "repository_review_tools_unavailable");
    assert_eq!(inline["harness"], "agy");
    fs::remove_file(state.join("inline-seat")).unwrap();
    fs::write(state.join("no-review-seat"), "").unwrap();
    let previous = json!({"head":head, "text":"VERDICT: REQUEST_CHANGES"});
    let deferred = host.review(&head, Some(&previous)).unwrap();
    assert_eq!(deferred["status"], "deferred_to_batch_seat");
    assert_eq!(
        deferred["test_replay"],
        json!(host.replay_receipt(&head).unwrap())
    );
    assert_eq!(deferred["causes"][0]["reason"], "limited");
    assert_eq!(deferred["causes"][1]["leg"], "mimo-v2.6-pro");
    fs::remove_file(state.join("no-review-seat")).unwrap();
    check_review_resume_after_missing_client(&mut host, &base, &head, &cli);
    check_late_review_reuses_completed_run(&mut host, &head, &cli);
    check_review_replaces_unreadable_run(&mut host, &head, &cli);
    write_review_transport_fixture(
        &cli,
        "import sys\nprint('wrapper failure', file=sys.stderr)\nsys.exit(10)\n",
    )
    .unwrap();
    let error = host.review(&head, None).unwrap_err();
    assert!(
        error.contains("review CLI failed with exit code 10: wrapper failure"),
        "{error}"
    );
    // A spawner that prints no run record is an error, never a verdict.
    host.review_cli = vec!["true".into()];
    host.review_override = Some(("fixture".into(), "explicit fixture seat".into()));
    assert!(host
        .review(&head, None)
        .unwrap_err()
        .starts_with("review run returned no record"));
    git(&cwd, &["update-ref", &host.base_pin(), &head]);
    assert!(!host.stamped(&head));
    assert!(host
        .review(&head, None)
        .unwrap_err()
        .contains("receipt does not match"));
    git(&cwd, &["update-ref", &host.base_pin(), &base]);
    super::super::review_tests::assert_item_trailers_bound_to_brief(&mut host, |args| {
        git(&cwd, args)
    });
}
