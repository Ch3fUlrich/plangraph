//! One status file per launch: no two runs, retries or look-alike keys share one.
use super::*;

/// The status file a batch brief names, parsed from its `PLANGRAPH-BATCH-INPUT-V1` payload.
fn brief_status_file(brief: &str) -> String {
    let (_, payload) = brief.split_once('\n').unwrap();
    let payload: Value = serde_json::from_str(payload).unwrap();
    payload["run"]["status_file"].as_str().unwrap().to_owned()
}
fn launch_paths(registry: &Registry) -> Vec<String> {
    registry
        .launches
        .iter()
        .map(|(card, brief)| {
            // The card names the file the native writer host records as writer_status_path
            // and the brief quotes that same file.
            let path = card["writer_status_path"].as_str().unwrap().to_owned();
            // A single-item brief is plain text and names no status file.
            if brief.starts_with("PLANGRAPH-BATCH-INPUT-V1") {
                assert_eq!(brief_status_file(brief), path);
            }
            path
        })
        .collect()
}

/// The review brief of `record`, rendered by the real Python renderer.
fn rendered_review(record: &Value) -> String {
    let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src");
    let out = std::process::Command::new("python3")
        .env("PYTHONPATH", src)
        .args([
            "-c",
            "import json,sys\nfrom plangraph.review.prompt import render_brief\nprint(render_brief('review', {'writer_run': json.loads(sys.argv[1])}))",
            &record.to_string(),
        ])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap()
}

#[test]
fn overlapping_batch_runs_write_their_own_status_file_and_each_review_quotes_only_its_own() {
    let reviews = std::env::temp_dir().join(format!("feeder-s2-{}", std::process::id()));
    std::fs::create_dir_all(&reviews).unwrap();
    let reviews_dir = reviews.to_str().unwrap().to_owned();
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/a.rs"]),
        item("BETA-1", 7, &["src/b.rs"]),
        item("BETA-2", 6, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let meet = Arc::new(Barrier::new(2));
    // Each host sees one family, so each launches a two-item batch; both hold their first
    // claim until the other has claimed: the runs overlap. Each fake writer writes its DONE
    // lines to the path the feeder handed it at launch.
    std::thread::scope(|scope| {
        for (name, own) in [("dell", &items[..2]), ("coding", &items[2..])] {
            let mut host = Host::new(&registry, &items);
            host.meet = Some(meet.clone());
            host.writes_status = true;
            let (doc, dir) = (ready(own.to_vec()), &reviews_dir);
            scope.spawn(move || drive_in(dir, &doc, &mut host, name, 2, false));
        }
    });
    let registry = registry.lock().unwrap();
    assert_eq!(registry.launches.len(), 2, "{:?}", registry.started);
    let paths = launch_paths(&registry);
    assert_ne!(paths[0], paths[1], "two runs must not share a status file");
    for (card, path) in registry.launches.iter().map(|l| &l.0).zip(&paths) {
        // The writer record the native host persists: the card's review dir and status path.
        let record = json!({"review_dir": reviews_dir, "writer_status_path": path});
        let review = rendered_review(&record);
        let own = card["item_keys"][0].as_str().unwrap();
        let rival = if own.contains("ALPHA") {
            "BETA"
        } else {
            "ALPHA"
        };
        assert!(review.contains(&format!("DONE {own} abc")), "{review}");
        assert!(!review.contains(rival), "{review}");
    }
    std::fs::remove_dir_all(&reviews).unwrap();
}

#[test]
fn a_status_name_that_is_already_taken_is_never_handed_to_a_second_launch() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    // The name this launch will want (epoch 100) is already reserved by another launch.
    let dry = drive(&ready(items.clone()), &mut host, "dell", 1, true);
    let wanted = dry["card"]["writer_status_path"]
        .as_str()
        .unwrap()
        .replace("-e0.txt", "-e100.txt");
    registry.lock().unwrap().statuses.insert(wanted.clone());
    drive(&ready(items), &mut host, "dell", 1, false);
    let registry = registry.lock().unwrap();
    let got = launch_paths(&registry)[0].clone();
    assert_eq!(got, wanted.replace("-e100.txt", "-e100-1.txt"));
}

#[test]
fn keys_that_normalise_alike_and_retries_never_share_a_status_file() {
    let items = vec![
        item("FOO.BAR-1", 9, &["src/a.rs"]),
        item("FOO_BAR-1", 8, &["src/b.rs"]),
    ];
    // Same slug, different raw keys.
    let paths: Vec<String> = items
        .iter()
        .map(|one| {
            let registry: Arc<Mutex<Registry>> = Arc::default();
            let mut host = Host::new(&registry, &items);
            drive(&ready(vec![one.clone()]), &mut host, "dell", 1, false);
            let paths = launch_paths(&registry.lock().unwrap());
            paths[0].clone()
        })
        .collect();
    assert_ne!(paths[0], paths[1]);
    // The same item launched twice (the first start failed and was released) gets a new file.
    let retry = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &retry);
    host.fail_start = true;
    drive(&ready(retry.clone()), &mut host, "dell", 1, false);
    host.fail_start = false;
    drive(&ready(retry), &mut host, "dell", 1, false);
    let registry = registry.lock().unwrap();
    assert_eq!(registry.launches.len(), 1);
    assert!(registry.epochs.get("plangraph:ALPHA-1").unwrap() > &100);
    let path = launch_paths(&registry)[0].clone();
    assert!(path.ends_with("-e101.txt"), "{path}");
}

#[test]
fn a_single_item_run_also_gets_its_own_status_file() {
    let items = vec![item("ALPHA-1", 9, &["src/a.rs"])];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let mut host = Host::new(&registry, &items);
    drive(&ready(items), &mut host, "dell", 1, false);
    let path = launch_paths(&registry.lock().unwrap())[0].clone();
    assert!(
        path.starts_with("/reviews/status-alpha-1-") && path.ends_with("-e100.txt"),
        "{path}"
    );
}

/// The cards and briefs of two overlapping feeder launches, with the claim epoch and key digest
/// of each status path normalised to `N` and `aaaaaaaa`: the fixture the native writer host test feeds through its
/// own record persistence (`reviews_rendered_from_saved_writer_records...`).
const OVERLAP_LAUNCHES: &str =
    include_str!("../../../pg-spawnd/tests/fixtures/feeder-overlap-launches.json");

fn normalised(text: &str) -> String {
    // `-e<digits>.txt` -> `-eN.txt`, wherever the status path appears.
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find("-e") {
        let digits = rest[i + 2..]
            .chars()
            .take_while(char::is_ascii_digit)
            .count();
        if digits > 0 && rest[i + 2 + digits..].starts_with(".txt") {
            out.push_str(&rest[..i]);
            out.push_str("-eN");
            rest = &rest[i + 2 + digits..];
        } else {
            out.push_str(&rest[..i + 2]);
            rest = &rest[i + 2..];
        }
    }
    // The key digest before it is random-looking hex; the fixture keeps a readable stand-in.
    let mut text = out + rest;
    while let Some(i) = text.find("-eN.txt") {
        let digest = i.checked_sub(9).filter(|&d| {
            text[d..i].starts_with('-') && text[d + 1..i].chars().all(|c| c.is_ascii_hexdigit())
        });
        match digest {
            Some(d) => text.replace_range(d..i + 7, "-aaaaaaaa-eK.txt"),
            None => text.replace_range(i..i + 7, "-eK.txt"),
        }
    }
    text.replace("-eK.txt", "-eN.txt")
}

#[test]
fn overlapping_feeder_launches_produce_the_cards_and_briefs_the_native_writer_test_consumes() {
    let items = vec![
        item("ALPHA-1", 9, &["src/a.rs"]),
        item("ALPHA-2", 8, &["src/a.rs"]),
        item("BETA-1", 7, &["src/b.rs"]),
        item("BETA-2", 6, &["src/b.rs"]),
    ];
    let registry: Arc<Mutex<Registry>> = Arc::default();
    let meet = Arc::new(Barrier::new(2));
    std::thread::scope(|scope| {
        for (name, own) in [("dell", &items[..2]), ("coding", &items[2..])] {
            let mut host = Host::new(&registry, &items);
            host.meet = Some(meet.clone());
            let doc = ready(own.to_vec());
            scope.spawn(move || drive(&doc, &mut host, name, 2, false));
        }
    });
    let mut launches: Vec<Value> = registry
        .lock()
        .unwrap()
        .launches
        .iter()
        .map(|(card, brief)| json!({"card": card, "brief": brief}))
        .collect();
    launches.sort_by_key(|l| l["card"]["item_key"].as_str().unwrap().to_owned());
    let actual: Value = serde_json::from_str(&normalised(&json!(launches).to_string())).unwrap();
    let golden: Value = serde_json::from_str(OVERLAP_LAUNCHES).unwrap();
    assert_eq!(
        actual.as_array().unwrap().len(),
        golden.as_array().unwrap().len()
    );
    for (actual, golden) in actual
        .as_array()
        .unwrap()
        .iter()
        .zip(golden.as_array().unwrap())
    {
        assert_eq!(actual["card"], golden["card"], "{actual:#}");
        // The brief is a version line plus a JSON payload carried inside a string: compare the
        // parsed payloads, since the key order of the embedded text depends on the build.
        let (version, payload) = payload_of(actual["brief"].as_str().unwrap());
        let (golden_version, golden_payload) = payload_of(golden["brief"].as_str().unwrap());
        assert_eq!(version, golden_version);
        assert_eq!(payload, golden_payload, "{actual:#}");
    }
}

/// The version line and the parsed JSON payload of a `PLANGRAPH-BATCH-INPUT-V1` brief.
fn payload_of(brief: &str) -> (String, Value) {
    let (version, payload) = brief.split_once('\n').unwrap();
    (version.to_owned(), serde_json::from_str(payload).unwrap())
}
