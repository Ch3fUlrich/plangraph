use pg_fleet::handoff::{check_text, log_done, read_card, Card};
use serde_json::Value;
use std::fs;
use std::process::Command;

fn card() -> String {
    let cases: Vec<Value> = serde_json::from_str(include_str!("fixtures/handoff.json")).unwrap();
    cases[0]["text"].as_str().unwrap().to_owned()
}
fn records(p: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(p)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
#[test]
fn log_fields_dedupe_and_evidence_identity() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("w.card.md");
    let wl = d.path().join("log/worklog.jsonl");
    fs::write(&p, card()).unwrap();
    let doc = Card::parse(&card());
    assert_eq!(log_done(&p, &doc, &wl).unwrap(), 2);
    let recs = records(&wl);
    assert_eq!(recs[0].as_object().unwrap().len(), 7);
    assert_eq!(recs[0]["ts"], "2026-10-31T04:44Z");
    assert_eq!(recs[0]["role"], "L1");
    assert_eq!(recs[0]["session"], "build-L1");
    assert_eq!(recs[0]["item"], "D-3294");
    assert_eq!(recs[0]["ref"], "07e60525");
    assert_eq!(recs[0]["evidence"], "CI run 41168126109 green");
    assert_eq!(log_done(&p, &doc, &wl).unwrap(), 0);
    let next = card().replace("pytest 212 passed", "pytest 213 passed");
    assert_eq!(log_done(&p, &Card::parse(&next), &wl).unwrap(), 1);
    assert_eq!(records(&wl).len(), 3);
}
#[test]
fn duplicate_none_null_ref_and_year_rollover() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("w.card.md");
    let wl = d.path().join("log");
    let base = card();
    let start = base.find("## Done\n").unwrap() + 8;
    let end = base.find("## In flight").unwrap();
    let build = |done: &str| {
        format!("{}{}{}", &base[..start], done, &base[end..])
            .replace("2026-10-31T05:10Z", "2027-01-02T10:00Z")
    };
    let none = Card::parse(&build("- none\n"));
    assert_eq!(log_done(&p, &none, &wl).unwrap(), 0);
    assert!(!wl.exists());
    let text=build("- 12-31T23:00Z D-1 - operator confirmed\n- 12-31T23:00Z D-1 - operator confirmed\n- 01-02T09:00Z D-2 0123456 ok\n- 01-03T00:30Z D-3 0123456 ok\n");
    assert!(check_text(&text).is_empty());
    assert_eq!(log_done(&p, &Card::parse(&text), &wl).unwrap(), 3);
    let recs = records(&wl);
    assert!(recs[0]["ref"].is_null());
    assert_eq!(
        recs.iter()
            .map(|r| r["ts"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "2026-12-31T23:00Z",
            "2027-01-02T09:00Z",
            "2027-01-03T00:30Z"
        ]
    );
}
#[test]
fn repair_missing_newline_and_skip_corrupt_records() {
    let d = tempfile::tempdir().unwrap();
    let wl = d.path().join("log");
    fs::write(&wl, "not json\n[]\n{\"item\":\"OLD\"}").unwrap();
    assert_eq!(
        log_done(&d.path().join("x"), &Card::parse(&card()), &wl).unwrap(),
        2
    );
    assert_eq!(fs::read_to_string(wl).unwrap().lines().count(), 5);
}
#[test]
fn read_bom_limit_secret_and_missing() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("x.card.md");
    fs::write(&p, format!("\u{feff}{}", card())).unwrap();
    assert!(check_text(&read_card(&p).unwrap()).is_empty());
    fs::write(&p, vec![b'x'; 65537]).unwrap();
    assert!(read_card(&p).unwrap_err().starts_with("card is larger"));
    assert!(read_card(&d.path().join("missing"))
        .unwrap_err()
        .starts_with("cannot read"));
    assert!(read_card(d.path()).is_err());
    for name in ["x.token", "x.dsn", ".credentials.json"] {
        let p = d.path().join(name);
        fs::write(&p, "CANARY").unwrap();
        assert_eq!(
            read_card(&p).unwrap_err(),
            format!("refusing to read {name}: secret-like file")
        );
    }
}
#[test]
fn cli_usage_failure_logging_and_default_home() {
    let d = tempfile::tempdir().unwrap();
    let p = d.path().join("w.card.md");
    fs::write(&p, card()).unwrap();
    let run = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_handoff-check"))
            .args(args)
            .env("HOME", d.path())
            .env_remove("FLEET_WORKLOG")
            .output()
            .unwrap()
    };
    for args in [vec![], vec!["a", "b"], vec!["--nope"]] {
        assert_eq!(run(&args).status.code(), Some(2));
    }
    let out = run(&["--log", p.to_str().unwrap()]);
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains("2 new in"));
    assert_eq!(
        records(&d.path().join(".local/state/fleet/worklog.jsonl")).len(),
        2
    );
    fs::write(&p, "# nope\n").unwrap();
    assert_eq!(run(&[p.to_str().unwrap(), "--log"]).status.code(), Some(1));
}
#[cfg(unix)]
#[test]
fn secret_symlink_loop_and_config_liveness() {
    use std::os::unix::fs::symlink;
    let d = tempfile::tempdir().unwrap();
    let secret = d.path().join("x.token");
    fs::write(&secret, "CANARY").unwrap();
    let p = d.path().join("innocent.card.md");
    symlink(&secret, &p).unwrap();
    assert!(read_card(&p).unwrap_err().starts_with("refusing"));
    let lp = d.path().join("loop");
    symlink(&lp, &lp).unwrap();
    assert!(read_card(&lp).unwrap_err().starts_with("refusing"));
    let cfg = d.path().join(".config/plangraph");
    fs::create_dir_all(cfg.join("liveness")).unwrap();
    for (rel, ok) in [("router.md", false), ("liveness/w.card.md", true)] {
        let p = cfg.join(rel);
        fs::write(&p, card()).unwrap();
        let out = Command::new(env!("CARGO_BIN_EXE_handoff-check"))
            .arg(p)
            .env("HOME", d.path())
            .output()
            .unwrap();
        assert_eq!(out.status.success(), ok);
    }
}
