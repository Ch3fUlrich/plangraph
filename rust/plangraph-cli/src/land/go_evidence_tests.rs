use super::go_evidence::*;
use super::go_verify::{verify, Pins};
use super::main_state::{Answer, State};
use crate::ci_watch::github::Github;
use serde_json::{json, Value};
use std::collections::BTreeMap;

/// The pinned workflow text: the file the verifier compiles in.
const WORKFLOW_TEXT: &str = include_str!("../../../../.github/workflows/tests.yml");

const REPO: &str = "owner/name";
const BASE: &str = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0";
const HEAD: &str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";
const TREE: &str = "7777777777777777777777777777777777777777";
const RUN: &str = "run-abababababababababababababababab";

fn encode(bytes: &[u8]) -> String {
    const A: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |n, (i, b)| n | (*b as u32) << (16 - 8 * i));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
        if out.len().is_multiple_of(60) {
            out.push('\n');
        }
    }
    out
}

/// A GitHub that answers from a table; `pulls/9` may answer differently on its second read.
struct Fake {
    table: BTreeMap<String, Value>,
    pull_reads: u32,
    moved_on_second_read: bool,
    run_attempt_on_second_read: u32,
}

fn jobs(attempt: u32) -> Vec<Value> {
    let pins = Pins::protected().unwrap();
    pins.matrix.tiers["self"]
        .keys()
        .map(|n| json!({"name": n, "conclusion": "success", "run_attempt": attempt}))
        .collect()
}

impl Fake {
    fn new() -> Fake {
        let mut t = BTreeMap::new();
        t.insert(
            "git/ref/heads/main".into(),
            json!({"object": {"sha": BASE}}),
        );
        t.insert(
            format!("git/commits/{HEAD}"),
            json!({"tree": {"sha": TREE}}),
        );
        t.insert(
            format!("compare/{BASE}...{HEAD}"),
            json!({"merge_base_commit": {"sha": BASE},
                   "files": [{"filename": "docs/x.md"}, {"filename": "rust/a.rs"}]}),
        );
        t.insert(
            format!("actions/workflows/tests.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"),
            json!({"workflow_runs": [
                {"id": 4, "head_sha": HEAD, "event": "pull_request", "run_attempt": 1, "path": ".github/workflows/tests.yml"},
                {"id": 5, "head_sha": HEAD, "event": "pull_request", "run_attempt": 2, "path": ".github/workflows/tests.yml"},
                {"id": 6, "head_sha": HEAD, "event": "pull_request", "run_attempt": 1, "path": ".github/workflows/other.yml"}]}),
        );
        t.insert(
            "actions/runs/5/attempts/2/jobs?per_page=100".into(),
            json!({"jobs": jobs(2)}),
        );
        t.insert(
            format!("contents/.github/workflows/tests.yml?ref={HEAD}"),
            json!({"encoding": "base64", "content": encode(WORKFLOW_TEXT.as_bytes())}),
        );
        Fake {
            table: t,
            pull_reads: 0,
            moved_on_second_read: false,
            run_attempt_on_second_read: 2,
        }
    }
}

impl Github for Fake {
    fn pages(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String> {
        self.page(endpoint, field)
    }
    fn page(&mut self, endpoint: &str, field: Option<&str>) -> Result<Vec<Value>, String> {
        let endpoint = endpoint
            .strip_suffix("?per_page=100")
            .filter(|_| endpoint.starts_with("compare/"))
            .unwrap_or(endpoint);
        let v = self
            .table
            .get(endpoint)
            .ok_or(format!("no fixture for {endpoint}"))?;
        let rows = field.map_or(v, |f| &v[f]);
        rows.as_array().cloned().ok_or("not rows".into())
    }
    fn log(&mut self, _: u64) -> Result<String, String> {
        Err("no logs".into())
    }
    fn rerun(&mut self, _: u64) -> Result<(), String> {
        Err("no reruns".into())
    }
    fn value(&mut self, endpoint: &str) -> Result<Value, String> {
        if endpoint == "pulls/9" {
            self.pull_reads += 1;
            let head = if self.moved_on_second_read && self.pull_reads > 1 {
                "c".repeat(40)
            } else {
                HEAD.to_owned()
            };
            return Ok(json!({"number": 9, "head": {"sha": head},
                             "base": {"ref": "main", "repo": {"full_name": REPO}}}));
        }
        if endpoint == "actions/runs/5" {
            return Ok(json!({"id": 5, "run_attempt": self.run_attempt_on_second_read}));
        }
        self.table
            .get(endpoint)
            .cloned()
            .ok_or(format!("no fixture for {endpoint}"))
    }
}

fn answer(tip: &str, state: State) -> Answer {
    Answer {
        tip: tip.into(),
        state,
        runs: vec![],
        bound_t: 0,
        at: 0,
    }
}

fn receipt() -> AdmitReceipt {
    admit_receipt(BASE, HEAD, "m1", RUN)
}

const ROWS: &str = "\
| D-100 | name | GO #9 GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | router | parts 2 |
| D-101 | name | NO GO #9 GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | router | |
| D-102 | name | GO #8 GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | router | |
| D-103 | other | GO #9 GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | router | |
| D-104 | name | (15:46Z) GO owner/name PR #9 GO-HEAD A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1A1 | router | |
| D-105 | name | GO #9 GO-HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb | router | |
| D-106 | name | GATE-EDIT .github/workflows/ GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | operator | |
| garbage | name | GO #9 GO-HEAD a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1 | router | |
";

fn inputs<'a>(rows: &'a str, receipt: &'a AdmitReceipt, main: &'a Answer) -> Inputs<'a> {
    Inputs {
        repo: REPO,
        pr: 9,
        base: BASE,
        train_sha: HEAD,
        manifest: "m1",
        receipt,
        rows,
        main,
    }
}

fn codes(e: &super::go_verify::Evidence) -> Vec<&'static str> {
    verify(e, &Pins::protected().unwrap())
        .iter()
        .map(|r| r.code)
        .collect()
}

#[test]
fn a_fixture_source_yields_evidence_that_verify_accepts() {
    let (r, main) = (receipt(), answer(BASE, State::Green));
    let e = collect(&mut Fake::new(), &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(codes(&e), Vec::<&str>::new(), "{e:?}");
    assert_eq!((e.ci.run_id, e.ci.attempt, e.ci.latest_attempt), (5, 2, 2));
    assert_eq!(e.ci.tier, "self");
    assert_eq!(e.tree, TREE);
    assert_eq!(e.changed_paths, ["docs/x.md", "rust/a.rs"]);
    // Only the GO rows of this repository and pull request are carried (a "NO GO", another pull request,
    // another project and a malformed id are not); a row at another head stays, and verify judges by head.
    let ids: Vec<_> = e.approvals.iter().map(|a| a.d_id.as_str()).collect();
    assert_eq!(ids, ["D-100", "D-104", "D-105"]);
    assert!(e.approvals.iter().all(|a| a.pr == 9 && a.repo == REPO));
    assert_eq!(
        e.approvals[1].head, HEAD,
        "a head compares case-insensitively"
    );
    assert_ne!(e.approvals[2].head, HEAD);
}

#[test]
fn a_head_that_moves_between_two_reads_is_refused() {
    let (r, main) = (receipt(), answer(BASE, State::Green));
    let mut gh = Fake::new();
    gh.moved_on_second_read = true;
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap_err();
    assert!(e.starts_with("moved_head"), "{e}");
}

#[test]
fn a_moved_main_between_the_state_read_and_the_ref_read_is_refused() {
    let (r, main) = (receipt(), answer(&"d".repeat(40), State::Green));
    let e = collect(&mut Fake::new(), &inputs(ROWS, &r, &main)).unwrap_err();
    assert!(e.starts_with("stale_base"), "{e}");
}

#[test]
fn a_rerun_during_collection_is_a_stale_attempt_for_verify() {
    let (r, main) = (receipt(), answer(BASE, State::Green));
    let mut gh = Fake::new();
    gh.run_attempt_on_second_read = 3;
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(codes(&e), ["stale_attempt"]);
}

#[test]
fn jobs_of_another_attempt_are_mixed_and_a_red_main_is_carried() {
    let (r, main) = (receipt(), answer(BASE, State::Pending));
    let mut gh = Fake::new();
    let mut rows = jobs(2);
    rows[0]["run_attempt"] = json!(1);
    gh.table.insert(
        "actions/runs/5/attempts/2/jobs?per_page=100".into(),
        json!({"jobs": rows}),
    );
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(codes(&e), ["mixed_attempt", "main_not_green"]);
}

#[test]
fn an_unpinned_workflow_text_and_a_missing_run_are_refused_by_verify_or_collect() {
    let (r, main) = (receipt(), answer(BASE, State::Green));
    let mut gh = Fake::new();
    gh.table.insert(
        format!("contents/.github/workflows/tests.yml?ref={HEAD}"),
        json!({"encoding": "base64", "content": encode(b"name: changed\n")}),
    );
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(codes(&e), ["workflow_unpinned"]);
    let mut gh = Fake::new();
    gh.table.insert(
        format!("actions/workflows/tests.yml/runs?head_sha={HEAD}&event=pull_request&per_page=100"),
        json!({"workflow_runs": []}),
    );
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap_err();
    assert!(e.contains("no tests.yml pull_request run"), "{e}");
}

#[test]
fn the_receipt_of_another_manifest_and_a_missing_approval_are_refused_by_verify() {
    let (main, r) = (
        answer(BASE, State::Green),
        admit_receipt(BASE, HEAD, "m2", RUN),
    );
    let e = collect(&mut Fake::new(), &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(codes(&e), ["replay_mismatch", "delta_unbound"]);
    let r = receipt();
    let e = collect(&mut Fake::new(), &inputs("", &r, &main)).unwrap();
    assert_eq!(codes(&e), ["approval_missing"]);
}

#[test]
fn a_gate_edit_row_is_a_grant_for_its_head_only() {
    let (r, main) = (receipt(), answer(BASE, State::Green));
    let mut gh = Fake::new();
    gh.table.insert(
        format!("compare/{BASE}...{HEAD}"),
        json!({"merge_base_commit": {"sha": BASE}, "files": [{"filename": ".github/workflows/tests.yml"}]}),
    );
    let e = collect(&mut gh, &inputs(ROWS, &r, &main)).unwrap();
    assert_eq!(e.grants.len(), 1);
    assert_eq!(codes(&e), Vec::<&str>::new());
    let no_grant = ROWS.replace("GATE-EDIT", "GATE-EDIT-NOT");
    let mut gh = Fake::new();
    gh.table.insert(
        format!("compare/{BASE}...{HEAD}"),
        json!({"merge_base_commit": {"sha": BASE}, "files": [{"filename": ".github/workflows/tests.yml"}]}),
    );
    let e = collect(&mut gh, &inputs(&no_grant, &r, &main)).unwrap();
    assert_eq!(codes(&e), ["operator_path_unauthorized"]);
}

#[test]
fn base64_decodes_what_github_sends() {
    assert_eq!(decode_base64("aGVsbG8gd29ybGQ=\n").unwrap(), b"hello world");
    assert_eq!(decode_base64(&encode(b"ab")).unwrap(), b"ab");
    assert!(decode_base64("a$b=").is_err());
}

#[test]
fn the_cli_refuses_missing_flags_and_the_receipt_round_trips() {
    let home = std::path::Path::new("/nonexistent");
    let none = BTreeMap::new();
    assert_eq!(run_cli(home, &none).unwrap_err(), "missing --pr");
    let r = receipt();
    let back: AdmitReceipt = serde_json::from_str(&serde_json::to_string(&r).unwrap()).unwrap();
    assert_eq!(back, r);
    assert_eq!(manifest_digest(b"x").len(), 64);
    assert_ne!(manifest_digest(b"x"), manifest_digest(b"y"));
}
