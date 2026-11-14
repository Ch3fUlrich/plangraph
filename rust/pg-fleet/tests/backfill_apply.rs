use pg_fleet::backfill_apply::{apply, Options};
use pg_fleet::executor::Executor;
use pg_fleet::process::Output;

#[derive(Default)]
struct Fake {
    calls: Vec<(String, Vec<String>)>,
    seat_code: Option<i32>,
    wrapper_refuse: Option<String>,
}
impl Executor for Fake {
    fn run(&mut self, p: &str, a: &[String], _: u64, combined: bool) -> Result<Output, String> {
        assert!(combined);
        self.calls.push((p.into(), a.into()));
        let (code, text) = if p == "python3" {
            (
                self.seat_code
                    .unwrap_or(if a.contains(&"--head".into()) { 7 } else { 0 }),
                "earlier output\nseat result\n",
            )
        } else if self.wrapper_refuse.as_ref().is_some_and(|v| a[1] == *v) {
            (2, "wrapper result\ndenied: exact refusal\n")
        } else {
            (0, "wrapper result\n")
        };
        Ok(Output {
            code,
            stdout: text.into(),
            stderr: String::new(),
        })
    }
    fn pause(&mut self, _: u64) {}
}
fn options(d: &std::path::Path) -> Options {
    let runs = d.join("runs");
    for name in ["run-abcd0000", "run-cdef"] {
        std::fs::create_dir_all(runs.join("nested").join(name)).unwrap();
    }
    std::fs::write(runs.join("nested/run-cdef/record.json"), "{}").unwrap();
    let rows = d.join("selected");
    std::fs::write(
        &rows,
        "sample:POST\nsample:CLOSE\nsample:UNKNOWN\nsample:MISSING\n",
    )
    .unwrap();
    Options {
        apply: true,
        go: Some("D-99".into()),
        rows: Some(rows),
        dry_run: false,
        runs: Some(runs),
        writer_family: Some("openai".into()),
        token_dir: Some(d.join("unread-tokens")),
        url: None,
        repo: d.join("repo"),
        pgk: d.join("pgk.sh"),
        python: "python3".into(),
    }
}
fn report() -> Vec<Vec<String>> {
    [
        vec![
            "sample:POST",
            "merge",
            "head",
            "D-3",
            "go-head",
            "run-abcd,run-cdef",
            "post-verdicts",
        ],
        vec![
            "sample:CLOSE",
            "merge",
            "head",
            "D-2",
            "go-head",
            "",
            "close-by-decision",
        ],
        vec!["sample:MISSING", "merge", "head", "", "", "", "no-go-row"],
    ]
    .into_iter()
    .map(|r| r.into_iter().map(str::to_owned).collect())
    .collect()
}
#[test]
fn all_three_flags_required() {
    for bits in 0..7 {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        o.apply = bits & 1 != 0;
        if bits & 2 == 0 {
            o.go = None
        }
        if bits & 4 == 0 {
            o.rows = None
        }
        let mut f = Fake::default();
        assert_eq!(apply(&o, &report(), &mut f), (0, String::new()));
        assert!(f.calls.is_empty());
    }
}
#[test]
fn argv_results_counts_and_refusals() {
    let d = tempfile::tempdir().unwrap();
    let o = options(d.path());
    let mut f = Fake::default();
    let (code, out) = apply(&o, &report(), &mut f);
    assert_eq!(code, 7);
    assert!(out.contains("sample:POST run-abcd rc=0 seat result\n"));
    assert!(out.contains("sample:POST run-cdef rc=7 seat result\n"));
    assert!(out.contains("sample:UNKNOWN refused: not in current report\n"));
    assert!(out.contains("sample:MISSING refused: no-go-row\n"));
    assert!(out.ends_with("sent=2 refused=2 failed=1\n"));
    assert_eq!(f.calls.len(), 4);
    assert!(!f.calls[0].1.contains(&"--head".into()));
    assert!(f.calls[1].1.ends_with(&["--head".into(), "head".into()]));
    assert!(f.calls[0].1.starts_with(&[
        o.repo
            .join("tools/seat_verdict.py")
            .to_string_lossy()
            .into_owned(),
        "--key".into(),
        "sample:POST".into(),
        "--seat-dir".into()
    ]));
    assert_eq!(
        &f.calls[2].1[1..],
        [
            "evidence",
            "sample:CLOSE",
            "--kind",
            "decision",
            "--decision",
            "D-99"
        ]
    );
    assert_eq!(&f.calls[3].1[1..], ["submit", "sample:CLOSE"]);
}
#[test]
fn wrapper_refusal_continues_and_first_nonzero_wins() {
    for verb in ["evidence", "submit"] {
        let d = tempfile::tempdir().unwrap();
        let o = options(d.path());
        std::fs::write(o.rows.as_ref().unwrap(), "sample:CLOSE\nsample:POST\n").unwrap();
        let mut f = Fake {
            wrapper_refuse: Some(verb.into()),
            ..Fake::default()
        };
        let (code, out) = apply(&o, &report(), &mut f);
        assert_eq!(code, 2);
        assert!(out.lines().any(|s| s == "denied: exact refusal"));
        assert!(out.contains("run-abcd rc=0 seat result"));
        assert!(out.ends_with("sent=1 refused=1 failed=1\n"));
    }
}
#[test]
fn dry_run_runs_no_commands_and_reports_refused_rows() {
    let d = tempfile::tempdir().unwrap();
    let mut o = options(d.path());
    o.dry_run = true;
    let mut f = Fake::default();
    let (code, out) = apply(&o, &report(), &mut f);
    assert_eq!(code, 0);
    assert!(f.calls.is_empty());
    assert!(
        out.contains("seat_verdict.py --key sample:POST")
            || out.contains("seat_verdict.py' --key sample:POST")
    );
    assert!(out.contains("evidence sample:CLOSE --kind decision --decision D-99"));
    assert!(out.ends_with("sent=0 refused=2 failed=0\n"));
}
#[test]
fn seat_exit_status_duplicate_keys_and_url() {
    for rc in [0, 1, 2, 3, 4, 7] {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        o.url = Some("https://keeper.example.invalid".into());
        std::fs::write(o.rows.as_ref().unwrap(), "sample:POST\n sample:POST \n").unwrap();
        let mut report = report();
        report[0][5] = "run-abcd".into();
        let mut f = Fake {
            seat_code: Some(rc),
            ..Fake::default()
        };
        let (code, out) = apply(&o, &report, &mut f);
        assert_eq!(code, rc);
        assert_eq!(f.calls.len(), 1);
        assert!(f.calls[0]
            .1
            .ends_with(&["--url".into(), o.url.clone().unwrap()]));
        assert!(out.ends_with(if rc == 0 {
            "sent=1 refused=0 failed=0\n"
        } else if [2, 4].contains(&rc) {
            "sent=0 refused=1 failed=0\n"
        } else {
            "sent=0 refused=0 failed=1\n"
        }));
    }
}
#[test]
fn missing_ambiguous_or_unconfigured_seats_are_refused() {
    for mode in ["missing", "ambiguous", "family", "tokens"] {
        let d = tempfile::tempdir().unwrap();
        let mut o = options(d.path());
        std::fs::write(o.rows.as_ref().unwrap(), "sample:POST\n").unwrap();
        let mut report = report();
        report[0][5] = "run-abcd".into();
        match mode {
            "missing" => o.runs = Some(d.path().join("absent")),
            "ambiguous" => {
                std::fs::create_dir(o.runs.as_ref().unwrap().join("run-abcd1111")).unwrap()
            }
            "family" => o.writer_family = None,
            "tokens" => o.token_dir = None,
            _ => {}
        }
        let mut f = Fake::default();
        let (code, out) = apply(&o, &report, &mut f);
        assert_eq!(code, 2);
        assert!(f.calls.is_empty());
        assert!(out.ends_with("sent=0 refused=1 failed=0\n"));
    }
}
