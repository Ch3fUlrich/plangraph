use pg_fleet::executor::Executor;
use pg_fleet::merge_part::{merge, Options};
use pg_fleet::process::Output;

#[derive(Default)]
struct Fake {
    calls: Vec<(String, Vec<String>)>,
    head: Option<String>,
    mergeable: Option<String>,
    gate: i32,
    missing: i32,
    claim: &'static str,
    force: i32,
    timeout: &'static str,
    failures: usize,
    attempts: usize,
}
impl Executor for Fake {
    fn run(
        &mut self,
        p: &str,
        args: &[String],
        timeout: u64,
        _combined: bool,
    ) -> Result<Output, String> {
        self.calls.push((p.into(), args.into()));
        let (mut code, mut stdout) = (0, String::new());
        if p == "gh" {
            if args[0] == "api" {
                self.attempts += 1;
                if self.attempts <= self.failures {
                    code = 1
                } else {
                    stdout = "b".repeat(40) + "\n";
                }
            } else {
                let field = &args[args.iter().position(|s| s == "--json").unwrap() + 1];
                stdout = match field.as_str() {
                    "headRefOid" => self.head.clone().unwrap_or("a".repeat(40)),
                    "mergeable" => self.mergeable.clone().unwrap_or("MERGEABLE".into()),
                    "mergeCommit" => "b".repeat(40),
                    "state" => "OPEN".into(),
                    _ => panic!("unexpected {field}"),
                } + "\n";
            }
        } else if p == "go-row-gate" {
            code = self.gate;
            stdout = if code == 0 {
                "GO-ROW-OK\n"
            } else {
                "STOP: no row\n"
            }
            .into();
        } else if p == "bash" && args[0] == "keeper" {
            assert_eq!(timeout, 10);
            let verb = args[1].as_str();
            if self.timeout == verb
                || (self.timeout == "stale" && args.get(2).is_some_and(|s| s == "--force-stale"))
            {
                return Err("TimeoutExpired".into());
            }
            if verb == "show" {
                code = self.missing;
                stdout = "PRIVATE\n".into();
            } else if args.get(2).is_some_and(|s| s == "--force-stale") {
                code = self.force;
                stdout = "claimed\n".into();
            } else {
                code = i32::from(!self.claim.is_empty());
                stdout = if self.claim.is_empty() {
                    "claimed\n".into()
                } else {
                    format!("{}\n", self.claim)
                };
            }
        }
        Ok(Output {
            code,
            stdout,
            stderr: String::new(),
        })
    }
    fn pause(&mut self, _seconds: u64) {}
}
fn options(root: &std::path::Path) -> Options {
    let body = root.join("body.md");
    std::fs::write(&body, "Plan-Item: p:ITEM\n").unwrap();
    Options {
        pr: "123".into(),
        want: "aaaaaaaa".into(),
        key: "p:ITEM".into(),
        go: "D-4665".into(),
        subject: "Merge subject".into(),
        body,
        repo: "example/repo".into(),
        session: Some("test-session".into()),
        no_claim: false,
        keeper: "keeper".into(),
        evidence: "evidence".into(),
        gate: "go-row-gate".into(),
        now: "12:34Z".into(),
        sleep: Some(0),
    }
}
#[test]
fn ordered_refusals_have_no_mutating_calls() {
    for (case, code) in [
        ("head", 2),
        ("gate", 5),
        ("keeper", 6),
        ("mergeable", 3),
        ("body", 4),
    ] {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        let mut f = Fake::default();
        match case {
            "head" => o.want = "deadbeef".into(),
            "gate" => f.gate = 5,
            "keeper" => f.missing = 1,
            "mergeable" => f.mergeable = Some("CONFLICTING".into()),
            "body" => std::fs::write(&o.body, "No item\n").unwrap(),
            _ => {}
        }
        let r = merge(&o, &mut f);
        assert_eq!(r.code, code, "{case}: {}", r.stdout);
        assert!(!f
            .calls
            .iter()
            .any(|(_, a)| a[0] == "api" || a.get(1).is_some_and(|s| s == "claim")));
        if ["head", "gate"].contains(&case) {
            assert!(!f.calls.iter().any(|(p, _)| p == "bash"));
        }
        assert!(!r.stdout.contains("PRIVATE"));
    }
}
#[test]
fn merge_pin_gate_key_claim_and_evidence_argv() {
    for no_claim in [false, true] {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        o.no_claim = no_claim;
        let mut f = Fake::default();
        let r = merge(&o, &mut f);
        assert_eq!(r.code, 0);
        let api = f.calls.iter().find(|(_, a)| a[0] == "api").unwrap();
        assert_eq!(
            &api.1[..4],
            ["api", "-X", "PUT", "repos/example/repo/pulls/123/merge"]
        );
        assert!(api.1.contains(&format!("sha={}", "a".repeat(40))));
        let gate = f.calls.iter().find(|(p, _)| p == "go-row-gate").unwrap();
        assert_eq!(
            gate.1,
            [
                "--go",
                "D-4665",
                "--head",
                &"a".repeat(40),
                "--session",
                "test-session",
                "--key",
                "p:ITEM"
            ]
        );
        assert_eq!(
            f.calls
                .iter()
                .any(|(_, a)| a.get(1).is_some_and(|s| s == "claim")),
            !no_claim
        );
        assert_eq!(f.calls.iter().any(|(_, a)| a[0] == "evidence"), !no_claim);
        assert!(r.stdout.contains(&format!(
            "MERGED 12:34Z #123 {} (GO D-4665)",
            "b".repeat(40)
        )));
    }
}
#[test]
fn stale_claim_timeout_and_failure_codes() {
    for (timeout, code) in [("show", 6), ("claim", 8), ("stale", 8)] {
        let d = tempfile::tempdir().unwrap();
        let o = options(d.path());
        let mut f = Fake {
            timeout,
            claim: if timeout == "stale" { "stale" } else { "" },
            ..Fake::default()
        };
        let r = merge(&o, &mut f);
        assert_eq!(r.code, code);
        assert!(r.stdout.contains("STOP: keeper timed out p:ITEM\n"));
        assert_eq!(f.attempts, 0);
    }
    for stale in [false, true] {
        let d = tempfile::tempdir().unwrap();
        let o = options(d.path());
        let mut f = Fake {
            claim: if stale { "stale" } else { "error" },
            force: 1,
            ..Fake::default()
        };
        assert_eq!(merge(&o, &mut f).code, 8);
        assert_eq!(f.attempts, 0);
    }
    let d = tempfile::tempdir().unwrap();
    let o = options(d.path());
    let mut f = Fake {
        claim: "stale",
        ..Fake::default()
    };
    assert_eq!(merge(&o, &mut f).code, 0);
    assert!(f.calls.iter().any(|(_, a)| a
        == &[
            "keeper",
            "claim",
            "--force-stale",
            "GO D-4665 names this item at aaaaaaaa",
            "p:ITEM"
        ]));
}
#[test]
fn retries_empty_heads_and_unknown_mergeability() {
    for (failures, code, attempts) in [(1, 0, 2), (4, 0, 5), (5, 7, 5)] {
        let d = tempfile::tempdir().unwrap();
        let o = options(d.path());
        let mut f = Fake {
            failures,
            ..Fake::default()
        };
        assert_eq!(merge(&o, &mut f).code, code);
        assert_eq!(f.attempts, attempts);
    }
    for (head, want) in [
        ("", ""),
        ("", "aaaaaaaa"),
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", ""),
    ] {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        o.want = want.into();
        let mut f = Fake {
            head: Some(head.into()),
            ..Fake::default()
        };
        assert_eq!(merge(&o, &mut f).code, 2);
        assert_eq!(f.attempts, 0);
    }
    let d = tempfile::tempdir().unwrap();
    let o = options(d.path());
    let mut f = Fake {
        mergeable: Some("UNKNOWN".into()),
        ..Fake::default()
    };
    assert_eq!(merge(&o, &mut f).code, 3);
    assert_eq!(
        f.calls
            .iter()
            .filter(|(_, a)| a.contains(&"mergeable".into()))
            .count(),
        6
    );
}
