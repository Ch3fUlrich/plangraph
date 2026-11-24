use super::*;
use serde_json::json;

struct Fixed(Vec<Lane>);
impl LaneSource for Fixed {
    fn lanes(&self, _: &[String]) -> Vec<Lane> {
        self.0.clone()
    }
}

fn held(key: &str) -> Fixed {
    Fixed(vec![Lane {
        name: "writer-0673".into(),
        keys: vec![key.into()],
        source: "run",
    }])
}

fn check_brief(
    brief: &str,
    source: &dyn LaneSource,
    override_reason: Option<&str>,
) -> Result<Option<String>, String> {
    admit(brief, &[], source, override_reason).map(|(reason, _)| reason)
}

const BRIEF: &str = "### demo:ALPHA | title\nWhy: x\n\n### demo:BETA | title\nWhy: y\n";

#[test]
fn refuses_a_key_of_another_live_lane_and_names_the_lane() {
    let error = check_brief(BRIEF, &held("demo:BETA"), None).unwrap_err();
    assert!(error.starts_with("lane_collision: demo:BETA is held by live lane writer-0673"));
    assert!(!error.contains("demo:ALPHA"));
}

#[test]
fn a_free_key_passes() {
    assert_eq!(check_brief(BRIEF, &held("demo:OTHER"), None), Ok(None));
}

#[test]
fn override_with_a_reason_passes_and_is_returned() {
    let reason = check_brief(BRIEF, &held("demo:ALPHA"), Some("  fix lane  "));
    assert_eq!(reason, Ok(Some("fix lane".into())));
}

#[test]
fn blank_override_is_refused_even_without_a_collision() {
    for blank in ["", "  \t"] {
        let error = check_brief(BRIEF, &held("demo:OTHER"), Some(blank)).unwrap_err();
        assert_eq!(error, "lane_override requires a nonblank reason");
    }
}

#[test]
fn an_override_line_in_the_brief_text_is_not_an_override() {
    let brief = format!("{BRIEF}LANE_OVERRIDE: second lane on purpose\n");
    assert!(check_brief(&brief, &held("demo:ALPHA"), None).is_err());
}

#[test]
fn live_runs_hold_their_item_key_brief_keys_and_recorded_items() {
    let runs = [
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ONE"}),
        json!({"run_id":"run-2","role":"implement",
               "manifest":{"brief":"### demo:TWO | t\nWhy\n"}}),
        json!({"run_id":"run-3","role":"implement","fleet_session_name":"writer-0009",
               "item_keys":["demo:THREE"]}),
        json!({"run_id":"run-4","role":"implement","item_key":"bad key"}),
        json!({"run_id":"run-5","role":"review","item_key":"demo:ONE"}),
    ];
    let lanes = run_lanes(&runs);
    let by: Vec<_> = lanes
        .iter()
        .map(|l| (l.name.as_str(), l.keys.clone()))
        .collect();
    assert_eq!(
        by,
        [
            ("run-1", vec!["demo:ONE".to_string()]),
            ("run-2", vec!["demo:TWO".to_string()]),
            ("writer-0009", vec!["demo:THREE".to_string()]),
        ]
    );
}

#[test]
fn only_queued_or_running_records_are_live() {
    let dir = tempfile::tempdir().unwrap();
    for (id, status) in [
        ("run-a", "running"),
        ("run-b", "completed"),
        ("run-c", "queued"),
    ] {
        let run = dir.path().join("runs").join(id);
        fs::create_dir_all(&run).unwrap();
        fs::write(
            run.join("record.json"),
            json!({"run_id":id,"status":status,"item_key":"demo:K"}).to_string(),
        )
        .unwrap();
    }
    let ids: Vec<_> = live_runs(dir.path())
        .iter()
        .map(|r| r["run_id"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(ids, ["run-a", "run-c"]);
}

fn git(dir: &Path, args: &[&str]) {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(["-c", "user.name=t", "-c", "user.email=t@example.test"])
        .args(args)
        .output()
        .unwrap();
    assert!(out.status.success(), "{args:?}: {out:?}");
}

#[test]
fn unmerged_remote_lane_branches_hold_the_key_slug() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/own-slug"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/own-slug", "HEAD"],
    );
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/fix-alpha-x", "HEAD"],
    );
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/merged-beta", "main"],
    );
    let keys = [
        "demo:ALPHA-X".to_owned(),
        "demo:BETA".into(),
        "demo:OWN-SLUG".into(),
    ];
    let lanes = branch_lanes(repo, &keys);
    assert_eq!(
        lanes,
        [Lane {
            name: "origin/lane/fix-alpha-x".into(),
            keys: vec!["demo:ALPHA-X".into()],
            source: "branch",
        }]
    );
}

#[test]
fn branch_slug_matches_whole_tokens_only() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in [
        "lane/fixture-a",
        "lane/prefix-1",
        "lane/fix-b",
        "lane/a.fix_c",
    ] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:FIX".to_owned()]);
    let mut names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    names.sort_unstable();
    assert_eq!(names, ["origin/lane/a.fix_c", "origin/lane/fix-b"]);
    // A slug of several tokens must appear in order and side by side.
    let keys = ["demo:ALPHA-X".to_owned()];
    git(
        repo,
        &["update-ref", "refs/remotes/origin/lane/x-alpha", "HEAD"],
    );
    assert!(branch_lanes(repo, &keys).is_empty());
}

fn request<'a>(state: &'a Path, extra: &'a [String]) -> LaunchRequest<'a> {
    LaunchRequest {
        prompt: "Brief\n",
        extra,
        state,
        repo: Path::new("/nonexistent"),
        explicit: None,
        reserve: true,
    }
}

fn reservation_files(state: &Path) -> usize {
    crate::lane_reservation::claim_files(state)
}

#[test]
fn refused_launch_releases_reservation() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:A".to_owned(), "demo:B".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    assert_eq!(reservation_files(dir.path()), 2);
    // A second launch is refused naming the reservation and reserves nothing new.
    let error = admit_launch(&request(dir.path(), &keys[1..])).unwrap_err();
    assert!(error.contains("held by live lane reservation"), "{error}");
    assert_eq!(reservation_files(dir.path()), 2);
    // The first launch fails after admission: dropping it frees the keys at once.
    drop(first);
    assert_eq!(reservation_files(dir.path()), 0);
    drop(admit_launch(&request(dir.path(), &keys[1..])).unwrap());
    assert_eq!(reservation_files(dir.path()), 0);
}

#[test]
fn a_check_only_request_reserves_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:A".to_owned()];
    let mut check = request(dir.path(), &keys);
    check.reserve = false;
    let launched = admit_launch(&check).unwrap();
    assert_eq!(launched.keys, keys);
    assert_eq!(reservation_files(dir.path()), 0);
    assert!(admit_launch(&request(dir.path(), &keys)).is_ok());
}

#[test]
fn two_concurrent_admissions_of_one_key_admit_one() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:RACE".to_owned()];
    let barrier = std::sync::Barrier::new(8);
    let results: Vec<Result<Launched, String>> = std::thread::scope(|scope| {
        let workers: Vec<_> = (0..8)
            .map(|_| {
                scope.spawn(|| {
                    barrier.wait();
                    admit_launch(&request(dir.path(), &keys))
                })
            })
            .collect();
        workers.into_iter().map(|w| w.join().unwrap()).collect()
    });
    assert_eq!(
        results.iter().filter(|r| r.is_ok()).count(),
        1,
        "{results:?}"
    );
    for refused in results.iter().filter_map(|r| r.as_ref().err()) {
        assert!(
            refused.contains("demo:RACE is held by live lane"),
            "{refused}"
        );
    }
}

#[test]
fn override_with_reason_is_recorded_and_blank_is_refused() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:HELD".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("  fix lane  ");
    let launched = admit_launch(&second).unwrap();
    assert_eq!(launched.lane_override.as_deref(), Some("fix lane"));
    assert_eq!(launched.keys, keys);
    for blank in ["", "  \t"] {
        second.explicit = Some(blank);
        let error = admit_launch(&second).unwrap_err();
        assert_eq!(error, "lane_override requires a nonblank reason");
    }
    drop((first, launched));
}

#[test]
fn dropping_an_override_launch_leaves_the_original_claim() {
    let dir = tempfile::tempdir().unwrap();
    let keys = ["demo:HELD".to_owned()];
    let first = admit_launch(&request(dir.path(), &keys)).unwrap();
    let mut second = request(dir.path(), &keys);
    second.explicit = Some("on purpose");
    let override_launch = admit_launch(&second).unwrap();
    assert_eq!(reservation_files(dir.path()), 2);
    // The override fails before it registers: the first launch is still pending and holds.
    drop(override_launch);
    assert_eq!(reservation_files(dir.path()), 1);
    let error = admit_launch(&request(dir.path(), &keys)).unwrap_err();
    assert!(error.contains("held by live lane reservation"), "{error}");
    drop(first);
    assert_eq!(reservation_files(dir.path()), 0);
}

#[test]
fn a_typed_prompt_override_does_not_admit_a_held_key() {
    let dir = tempfile::tempdir().unwrap();
    let run = dir.path().join("runs/run-held");
    fs::create_dir_all(&run).unwrap();
    fs::write(
        run.join("record.json"),
        json!({"run_id":"run-held","status":"running","role":"implement",
               "item_key":"demo:FIXTURE"})
        .to_string(),
    )
    .unwrap();
    let mut input: Value = serde_json::from_str(
        include_str!("../tests/fixtures/batch-writer-input.txt")
            .replace("\r\n", "\n")
            .strip_prefix("PLANGRAPH-BATCH-INPUT-V1\n")
            .unwrap(),
    )
    .unwrap();
    input["lane_override"] = json!("typed reason");
    let prompt = format!("PLANGRAPH-BATCH-INPUT-V1\n{input}");
    let mut typed = request(dir.path(), &[]);
    typed.prompt = &prompt;
    let error = admit_launch(&typed).unwrap_err();
    assert!(
        error.contains("demo:FIXTURE is held by live lane run-held"),
        "{error}"
    );
    // The card field is the override a dispatch admits.
    typed.explicit = Some("card reason");
    let launched = admit_launch(&typed).unwrap();
    assert_eq!(launched.lane_override.as_deref(), Some("card reason"));
}

#[test]
fn a_key_with_a_second_colon_is_one_key_in_briefs_runs_and_cards() {
    assert_eq!(
        item_keys("### demo:ITEM:detail | t\nWhy\n\n## demo:KEY: title\n"),
        ["demo:ITEM:detail", "demo:KEY"]
    );
    assert_eq!(normalize("demo:demo:ITEM:detail"), "demo:ITEM:detail");
    let runs = [json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:detail"})];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:detail"]);
    // After registration the full key is held and its prefix is not.
    let held = Fixed(lanes);
    let full = ["demo:ITEM:detail".to_owned()];
    assert!(admit("", &full, &held, None).is_err());
    let short = ["demo:ITEM".to_owned()];
    assert!(admit("", &short, &held, None).is_ok());
}

#[test]
fn the_remote_name_is_not_part_of_a_branch_slug() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in ["lane/unrelated", "lane/origin-fix"] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:ORIGIN".to_owned()]);
    let names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    assert_eq!(names, ["origin/lane/origin-fix"]);
}

#[test]
fn record_keys_are_read_by_the_key_grammar_and_headings_by_their_separator() {
    // A run holds `demo:ITEM:` as stored: the heading parser must not trim it.
    let runs = [
        json!({"run_id":"run-1","role":"implement","item_key":"demo:ITEM:",
                       "item_keys":["demo:demo:OTHER","bad key","demo:"]}),
    ];
    let lanes = run_lanes(&runs);
    assert_eq!(lanes[0].keys, ["demo:ITEM:", "demo:OTHER"]);
    let held = Fixed(lanes);
    assert!(admit("", &["demo:ITEM:".to_owned()], &held, None).is_err());
    assert!(admit("", &["demo:ITEM".to_owned()], &held, None).is_ok());
    // A longest valid id still ends at the colon of the heading's title.
    let long = format!("demo:{}", "a".repeat(180));
    assert!(valid_key(&long));
    assert_eq!(
        item_keys(&format!("### {long}: title\n")),
        std::slice::from_ref(&long)
    );
    assert_eq!(item_keys(&format!("## {long} | title\n")), [long]);
    let too_long = format!("demo:{}", "a".repeat(181));
    assert!(item_keys(&format!("### {too_long}: title\n")).is_empty());
}

#[test]
fn a_branch_holds_a_key_with_a_second_colon_by_all_its_id_tokens() {
    let dir = tempfile::tempdir().unwrap();
    let repo = dir.path();
    git(repo, &["init", "-q", "-b", "main"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "base"]);
    git(repo, &["update-ref", "refs/remotes/origin/main", "HEAD"]);
    git(repo, &["checkout", "-q", "-b", "lane/mine"]);
    git(repo, &["commit", "-q", "--allow-empty", "-m", "own"]);
    for branch in ["lane/item-detail", "lane/detail-only"] {
        git(
            repo,
            &[
                "update-ref",
                &format!("refs/remotes/origin/{branch}"),
                "HEAD",
            ],
        );
    }
    let lanes = branch_lanes(repo, &["demo:ITEM:detail".to_owned()]);
    let names: Vec<_> = lanes.iter().map(|lane| lane.name.as_str()).collect();
    assert_eq!(names, ["origin/lane/item-detail"]);
}
