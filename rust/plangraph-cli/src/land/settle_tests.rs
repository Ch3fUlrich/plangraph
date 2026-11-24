use super::settle::*;
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

const MERGE: &str = "m000000000000000000000000000000000000000";

fn sha(c: char) -> String {
    std::iter::repeat_n(c, 40).collect()
}

fn part(pr: u64, folded: bool) -> PartState {
    let c = char::from(b'a' + pr as u8);
    PartState {
        pr,
        head: sha(c),
        items: vec![format!("plangraph:ITEM-{pr}")],
        folded,
        writer_head: Some(sha(c)),
        writer_record: Some(PathBuf::from(format!("/runs/{pr}"))),
        owner: Some("writer".into()),
        ready_at: Some(100),
        close_items: Step::Pending,
        close_pr: Step::Pending,
        stop_writer: Step::Pending,
    }
}

/// Fake keeper, GitHub and hosts. `fail_at` makes the n-th effect call fail before it changes anything
/// (a crash); `landed` is the set of commits inside the merge.
#[derive(Default)]
struct Fake {
    calls: usize,
    fail_at: Option<usize>,
    heads: BTreeMap<u64, (PrState, String)>,
    landed: BTreeSet<String>,
    /// A push that lands while the close call runs.
    push_during_close: BTreeMap<u64, String>,
    host_receipts: Vec<HostReceipt>,
    proofs: Vec<(u64, Proof)>,
    closed_items: Vec<u64>,
    closed_prs: Vec<u64>,
    reopened: Vec<u64>,
    stopped: Vec<u64>,
    holds_ended: usize,
    folds: Vec<(u64, String)>,
    keeper_keeps: BTreeMap<u64, String>,
    stop_keeps: BTreeMap<u64, String>,
}

impl Fake {
    fn new(parts: &[PartState]) -> Fake {
        let mut f = Fake::default();
        for p in parts {
            let state = if p.folded {
                PrState::Open
            } else {
                PrState::Merged
            };
            f.heads.insert(p.pr, (state, p.head.clone()));
            f.landed.insert(p.head.clone());
        }
        f.host_receipts = ["ws", "wsl", "dell"]
            .iter()
            .map(|h| HostReceipt {
                host: (*h).into(),
                head: MERGE.into(),
            })
            .collect();
        f
    }

    fn tick(&mut self) -> Result<(), String> {
        self.calls += 1;
        if self.fail_at == Some(self.calls) {
            return Err("crash".into());
        }
        Ok(())
    }
}

impl Effects for Fake {
    fn fold_docs(&mut self, train_pr: u64, merge: &str) -> Result<String, String> {
        self.tick()?;
        self.folds.push((train_pr, merge.to_owned()));
        Ok("folded 0 fragments".into())
    }

    fn pr_head(&mut self, pr: u64) -> Result<(PrState, String), String> {
        self.tick()?;
        Ok(self.heads[&pr].clone())
    }
    fn in_landed_tree(&mut self, sha: &str, _merge: &str) -> Result<bool, String> {
        self.tick()?;
        Ok(self.landed.contains(sha))
    }
    fn close_items(&mut self, part: &PartState, proof: &Proof) -> Result<Applied, String> {
        self.tick()?;
        if let Some(why) = self.keeper_keeps.get(&part.pr) {
            return Ok(Applied::Kept(why.clone()));
        }
        self.proofs.push((part.pr, proof.clone()));
        self.closed_items.push(part.pr);
        Ok(Applied::Done)
    }
    fn verify_hosts(&mut self, _merge: &str) -> Result<Vec<HostReceipt>, String> {
        self.tick()?;
        Ok(self.host_receipts.clone())
    }
    fn close_pr(&mut self, pr: u64) -> Result<(), String> {
        self.tick()?;
        if let Some(head) = self.push_during_close.remove(&pr) {
            self.heads.get_mut(&pr).unwrap().1 = head;
        }
        self.heads.get_mut(&pr).unwrap().0 = PrState::Closed;
        self.closed_prs.push(pr);
        Ok(())
    }
    fn reopen_pr(&mut self, pr: u64) -> Result<(), String> {
        self.tick()?;
        self.heads.get_mut(&pr).unwrap().0 = PrState::Open;
        self.reopened.push(pr);
        Ok(())
    }
    fn stop_writer(&mut self, part: &PartState, _merge: &str) -> Result<Applied, String> {
        self.tick()?;
        if let Some(why) = self.stop_keeps.get(&part.pr) {
            return Ok(Applied::Kept(why.clone()));
        }
        self.stopped.push(part.pr);
        Ok(Applied::Done)
    }
    fn end_hold(&mut self) -> Result<(), String> {
        self.tick()?;
        self.holds_ended += 1;
        Ok(())
    }
}

fn settle(parts: Vec<PartState>) -> Settlement {
    Settlement::new("example/repo", 9, MERGE, parts)
}

fn run(s: &mut Settlement, path: &std::path::Path, fx: &mut Fake) -> Result<Summary, String> {
    s.run(path, fx, &|| 1000)
}

#[test]
fn crash_at_every_effect_resumes_and_finishes_each_step_once() {
    let parts = vec![part(1, true), part(2, true), part(3, false)];
    // The number of effect calls of a clean run bounds the crash points.
    let dir = tempfile::tempdir().unwrap();
    let mut clean = Fake::new(&parts);
    let total = {
        let mut s = settle(parts.clone());
        assert!(
            run(&mut s, &dir.path().join("clean.json"), &mut clean)
                .unwrap()
                .complete
        );
        clean.calls
    };
    for crash in 1..=total {
        let path = dir.path().join(format!("s{crash}.json"));
        let mut fx = Fake::new(&parts);
        fx.fail_at = Some(crash);
        let mut s = settle(parts.clone());
        assert_eq!(
            run(&mut s, &path, &mut fx),
            Err("crash".into()),
            "crash {crash}"
        );
        // A new process: the state comes from the file, the effects keep what they already did.
        fx.fail_at = None;
        let mut resumed = Settlement::load(&path)
            .unwrap()
            .expect("state saved before cleanup");
        let summary = run(&mut resumed, &path, &mut fx).unwrap();
        assert!(
            summary.complete && summary.held.is_empty(),
            "crash {crash}: {summary:?}"
        );
        assert_eq!(fx.closed_items, vec![1, 2, 3], "crash {crash}");
        assert_eq!(fx.closed_prs, vec![1, 2], "crash {crash}");
        assert_eq!(fx.stopped, vec![1, 2, 3], "crash {crash}");
        assert_eq!(fx.holds_ended, 1, "crash {crash}");
        assert_eq!(
            fx.folds,
            vec![(9, MERGE.to_owned())],
            "once per train, crash {crash}"
        );
        // A third run changes nothing.
        let before = fx.calls;
        run(&mut resumed, &path, &mut fx).unwrap();
        assert_eq!(fx.calls, before, "a finished settlement calls no effect");
    }
}

#[test]
fn state_is_on_disk_before_the_first_effect() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("s.json");
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    fx.fail_at = Some(1);
    let mut s = settle(parts.clone());
    assert!(run(&mut s, &path, &mut fx).is_err());
    let saved = Settlement::load(&path).unwrap().unwrap();
    assert_eq!(saved.merge, MERGE);
    assert_eq!(saved.parts, parts);
}

#[test]
fn a_folded_part_closes_through_the_train_merge() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true), part(2, false)];
    let mut fx = Fake::new(&parts);
    let mut s = settle(parts);
    run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert_eq!(
        fx.proofs,
        vec![
            (
                1,
                Proof::TrainMerge {
                    merge: MERGE.into(),
                    part_head: sha('b')
                }
            ),
            (2, Proof::OwnMerge),
        ]
    );
}

#[test]
fn a_folded_part_outside_the_train_merge_keeps_its_item_open() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    fx.landed.clear();
    let mut s = settle(parts);
    let summary = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert!(fx.closed_items.is_empty());
    assert!(!summary.complete);
    assert!(
        summary.held[0].contains("not in the train merge"),
        "{summary:?}"
    );
}

#[test]
fn the_item_stays_open_when_its_pull_request_moved() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    fx.heads.get_mut(&1).unwrap().1 = sha('z');
    let mut s = settle(parts);
    let summary = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert!(fx.closed_items.is_empty() && fx.closed_prs.is_empty() && fx.stopped.is_empty());
    assert!(
        summary.held.iter().any(|h| h.contains("moved to")),
        "{summary:?}"
    );
}

#[test]
fn a_writer_with_unmerged_commits_is_not_stopped() {
    let dir = tempfile::tempdir().unwrap();
    let mut p = part(1, true);
    p.writer_head = Some(sha('y'));
    let mut fx = Fake::new(std::slice::from_ref(&p));
    let mut s = settle(vec![p]);
    let summary = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert!(fx.stopped.is_empty());
    assert!(
        fx.closed_items.is_empty(),
        "unmerged writer work keeps the item open"
    );
    assert!(
        summary.held.iter().any(|h| h.contains("unmerged work")),
        "{summary:?}"
    );
    assert!(!summary.complete && fx.holds_ended == 0);
}

#[test]
fn a_push_during_the_close_is_not_lost() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("s.json");
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    fx.push_during_close.insert(1, sha('q'));
    let mut s = settle(parts);
    let summary = run(&mut s, &path, &mut fx).unwrap();
    assert_eq!(fx.reopened, vec![1], "the pull request is given back");
    assert_eq!(fx.heads[&1], (PrState::Open, sha('q')));
    assert!(fx.stopped.is_empty(), "its writer keeps running");
    assert!(
        summary.held.iter().any(|h| h.contains("during close")),
        "{summary:?}"
    );
}

#[test]
fn a_push_before_the_close_leaves_the_pull_request_open() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    fx.heads.get_mut(&1).unwrap().1 = sha('q');
    let mut s = settle(parts);
    // Items hold first (the head moved), and the pull request is never closed.
    run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert!(fx.closed_prs.is_empty() && fx.reopened.is_empty());
}

#[test]
fn a_held_step_runs_when_its_reason_clears() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("s.json");
    let mut p = part(1, true);
    p.writer_head = Some(sha('y'));
    let mut fx = Fake::new(std::slice::from_ref(&p));
    let mut s = settle(vec![p]);
    assert!(!run(&mut s, &path, &mut fx).unwrap().complete);
    fx.landed.insert(sha('y'));
    let mut s = Settlement::load(&path).unwrap().unwrap();
    assert!(run(&mut s, &path, &mut fx).unwrap().complete);
    assert_eq!((fx.closed_items.len(), fx.stopped.len()), (1, 1));
}

#[test]
fn host_sync_needs_three_distinct_current_hosts() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, false)];
    for bad in [
        vec![HostReceipt {
            host: "ws".into(),
            head: MERGE.into(),
        }],
        vec![
            HostReceipt {
                host: "ws".into(),
                head: MERGE.into(),
            },
            HostReceipt {
                host: "ws".into(),
                head: MERGE.into(),
            },
            HostReceipt {
                host: "dell".into(),
                head: MERGE.into(),
            },
        ],
        vec![
            HostReceipt {
                host: "ws".into(),
                head: MERGE.into(),
            },
            HostReceipt {
                host: "wsl".into(),
                head: MERGE.into(),
            },
            HostReceipt {
                host: "dell".into(),
                head: sha('0'),
            },
        ],
    ] {
        let mut fx = Fake::new(&parts);
        fx.host_receipts = bad;
        let mut s = settle(parts.clone());
        let err = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap_err();
        assert!(err.contains("host sync"), "{err}");
        assert_eq!(fx.holds_ended, 0);
        assert!(!s.hosts.is_done());
    }
}

#[test]
fn events_record_what_happened() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true)];
    let mut fx = Fake::new(&parts);
    let mut s = settle(parts);
    s.record("ci", "run 7 green", 5);
    run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    let kinds: Vec<&str> = s.events.iter().map(|e| e.kind.as_str()).collect();
    assert_eq!(
        kinds,
        [
            "ci",
            "items_closed",
            "host_sync",
            "pr_closed",
            "writer_stopped",
            "docs_fold",
            "hold_released"
        ]
    );
}

#[test]
fn an_item_the_keeper_kept_open_and_a_writer_the_guard_kept_stay_held() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true), part(2, true)];
    let mut fx = Fake::new(&parts);
    fx.keeper_keeps.insert(1, "seats missing".into());
    fx.stop_keeps.insert(2, "unpushed commits".into());
    let mut s = settle(parts);
    let summary = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap();
    assert_eq!(fx.closed_items, vec![2]);
    // Its work is in the landed tree, so the writer of part 1 stops even though the keeper kept its item.
    assert_eq!(fx.stopped, vec![1]);
    assert!(!summary.complete && fx.holds_ended == 0);
    assert!(
        summary
            .held
            .iter()
            .any(|h| h.contains("#1 close_items") && h.contains("seats missing")),
        "{summary:?}"
    );
    assert!(
        summary
            .held
            .iter()
            .any(|h| h.contains("#2 stop_writer") && h.contains("unpushed commits")),
        "{summary:?}"
    );
    // The keeper closes it later; the next run finishes it and stops its writer.
    fx.keeper_keeps.clear();
    fx.stop_keeps.clear();
    let mut s = Settlement::load(&dir.path().join("s.json"))
        .unwrap()
        .unwrap();
    assert!(
        run(&mut s, &dir.path().join("s.json"), &mut fx)
            .unwrap()
            .complete
    );
    assert_eq!(fx.closed_items, vec![2, 1]);
    assert_eq!(fx.stopped, vec![1, 2]);
}

#[test]
fn a_push_after_a_crashed_close_is_given_back_on_resume() {
    let dir = tempfile::tempdir().unwrap();
    let parts = vec![part(1, true)];
    let mut probe = Fake::new(&parts);
    probe.push_during_close.insert(1, sha('q'));
    run(
        &mut settle(parts.clone()),
        &dir.path().join("probe.json"),
        &mut probe,
    )
    .unwrap();
    // Crash at every effect call; the one right after close_pr leaves the pull request closed with the
    // push on it, and the resumed run must give it back.
    let mut checked = false;
    for crash in 1..=probe.calls {
        let path = dir.path().join(format!("c{crash}.json"));
        let mut fx = Fake::new(&parts);
        fx.push_during_close.insert(1, sha('q'));
        fx.fail_at = Some(crash);
        let _ = run(&mut settle(parts.clone()), &path, &mut fx);
        if fx.heads[&1] != (PrState::Closed, sha('q')) {
            continue;
        }
        fx.fail_at = None;
        let mut resumed = Settlement::load(&path).unwrap().unwrap();
        run(&mut resumed, &path, &mut fx).unwrap();
        assert_eq!(
            fx.heads[&1],
            (PrState::Open, sha('q')),
            "the push is not lost"
        );
        assert_eq!(fx.reopened, vec![1]);
        checked = true;
    }
    assert!(
        checked,
        "some crash point leaves the pull request closed with the push"
    );
}

#[test]
fn a_writer_record_without_a_writer_head_is_an_error_not_no_writer() {
    let dir = tempfile::tempdir().unwrap();
    let mut p = part(1, false);
    p.writer_head = None;
    let mut fx = Fake::new(std::slice::from_ref(&p));
    let mut s = settle(vec![p]);
    let err = run(&mut s, &dir.path().join("s.json"), &mut fx).unwrap_err();
    assert!(err.contains("writer record but no writer head"), "{err}");
    assert!(fx.stopped.is_empty());
}
