use super::settle::{Applied, HostReceipt, PrState, Settlement};
use super::settle_cli::{parse_close_report, parse_hosts, parse_pr_view, parse_stop_report, run};
use std::collections::BTreeMap;

fn flags(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(k, v)| ((*k).into(), (*v).into()))
        .collect()
}

#[test]
fn gh_pull_request_state_is_parsed() {
    let head = "a".repeat(40);
    for (word, state) in [
        ("OPEN", PrState::Open),
        ("CLOSED", PrState::Closed),
        ("MERGED", PrState::Merged),
    ] {
        let raw = format!(r#"{{"state":"{word}","headRefOid":"{head}"}}"#);
        assert_eq!(parse_pr_view(&raw), Ok((state, head.clone())));
    }
    assert!(parse_pr_view(r#"{"state":"DRAFT","headRefOid":"x"}"#).is_err());
    assert!(parse_pr_view("not json").is_err());
}

#[test]
fn host_receipts_are_parsed_from_verify_hosts() {
    let head = "b".repeat(40);
    let raw = format!(
        r#"{{"verified":[{{"host":"one","head":"{head}","synced":false}},{{"host":"two","head":"{head}"}}],"count":2}}"#
    );
    assert_eq!(
        parse_hosts(&raw).unwrap(),
        vec![
            HostReceipt {
                host: "one".into(),
                head: head.clone()
            },
            HostReceipt {
                host: "two".into(),
                head
            },
        ]
    );
    assert!(parse_hosts(r#"{"verified":[{"host":"one"}]}"#).is_err());
}

fn init_file(dir: &std::path::Path, merge: &str, parts: &str) -> String {
    let path = dir.join("init.json");
    std::fs::write(
        &path,
        format!(r#"{{"repo":"example/repo","train_pr":9,"merge":"{merge}","parts":{parts}}}"#),
    )
    .unwrap();
    path.to_string_lossy().into_owned()
}

#[test]
fn init_refuses_a_short_sha_and_a_duplicate_part() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("s.json");
    let state = state.to_string_lossy().into_owned();
    let long = "c".repeat(40);
    let part = |pr: u64| format!(r#"{{"pr":{pr},"head":"{long}","items":["k:A"],"folded":true}}"#);
    let short = init_file(dir.path(), "abc", "[]");
    let err = run(&flags(&[
        ("state", &state),
        ("init", &short),
        ("repo-dir", "."),
    ]))
    .unwrap_err();
    assert!(err.contains("full lower-case SHA"), "{err}");
    let dup = init_file(dir.path(), &long, &format!("[{},{}]", part(1), part(1)));
    let err = run(&flags(&[
        ("state", &state),
        ("init", &dup),
        ("repo-dir", "."),
    ]))
    .unwrap_err();
    assert!(err.contains("twice"), "{err}");
    assert!(Settlement::load(std::path::Path::new(&state))
        .unwrap()
        .is_none());
}

#[test]
fn a_settlement_with_no_parts_needs_no_effect_but_the_hosts() {
    // The state file is the entry point: a saved settlement resumes without --init.
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("s.json");
    let mut done = Settlement::new("example/repo", 9, &"d".repeat(40), vec![]);
    done.hosts = super::settle::Step::Done("3 hosts".into());
    done.docs = super::settle::Step::Done("nothing to fold".into());
    done.hold_released = true;
    done.save(&state).unwrap();
    let lines = run(&flags(&[
        ("state", &state.to_string_lossy()),
        ("repo-dir", "."),
    ]))
    .unwrap();
    assert_eq!(lines, vec!["settle #9: 0 steps done, 0 held, complete"]);
}

#[test]
fn every_item_of_the_part_needs_a_closed_or_settled_done_row() {
    let report = "closed\tk:A\tabc\nlisted\tk:B\topen\tseats missing\nlisted\tk:C\topen\tother\nsettled\tk:D\tdone\n";
    let items = |keys: &[&str]| keys.iter().map(|k| (*k).to_owned()).collect::<Vec<_>>();
    assert_eq!(
        parse_close_report(report, &items(&["k:A", "k:D"])),
        Applied::Done
    );
    assert_eq!(
        parse_close_report(report, &items(&["k:A", "k:B"])),
        Applied::Kept("k:B listed open seats missing".into())
    );
    assert_eq!(parse_close_report("", &items(&[])), Applied::Done);
    for missing in ["", "merged-part\tk:A\t5\tabc\n", "settled\tk:A\topen\n"] {
        assert!(
            matches!(
                parse_close_report(missing, &items(&["k:A"])),
                Applied::Kept(_)
            ),
            "{missing:?}"
        );
    }
}

#[test]
fn a_writer_stop_that_kept_the_writer_is_kept_whatever_the_exit_code() {
    assert_eq!(
        parse_stop_report(r#"[{"status":"stopped","run_id":"r"}]"#),
        Ok(Applied::Done)
    );
    assert_eq!(
        parse_stop_report(
            r#"[{"status":"kept","record":"/r","reason":"writer branch has unpushed commits"}]"#
        ),
        Ok(Applied::Kept("writer branch has unpushed commits".into()))
    );
    assert!(parse_stop_report("[]").is_err());
    assert!(parse_stop_report(r#"[{"reason":"x"}]"#).is_err());
    assert!(parse_stop_report("nope").is_err());
}

#[test]
fn deployment_previews_and_scan_errors_keep_the_items_open() {
    let items = vec!["k:A".to_owned()];
    for report in [
        "deployment-pending\tk:A\t3h\tdell:unit\n",
        "would-close\tk:A\tabc\n",
        "scan-error\texample/repo\tcannot read main\n",
    ] {
        assert!(
            matches!(parse_close_report(report, &items), Applied::Kept(_)),
            "{report}"
        );
    }
    assert_eq!(
        parse_close_report("deployment-pending\tk:Z\t3h\tx\nclosed\tk:A\ta\n", &items),
        Applied::Done
    );
}

#[test]
fn init_needs_a_writer_head_and_record_together() {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("s.json");
    let state = state.to_string_lossy().into_owned();
    let long = "c".repeat(40);
    for part in [
        format!(r#"{{"pr":1,"head":"{long}","items":[],"folded":true,"writer_head":"{long}"}}"#),
        format!(r#"{{"pr":1,"head":"{long}","items":[],"folded":true,"writer_record":"/runs/1"}}"#),
    ] {
        let init = init_file(dir.path(), &long, &format!("[{part}]"));
        let err = run(&flags(&[
            ("state", &state),
            ("init", &init),
            ("repo-dir", "."),
        ]))
        .unwrap_err();
        assert!(err.contains("both writer_head and writer_record"), "{err}");
    }
    assert!(Settlement::load(std::path::Path::new(&state))
        .unwrap()
        .is_none());
}

#[cfg(unix)]
#[test]
fn the_real_adapter_scopes_the_keeper_close_to_the_part_and_reads_its_rows() {
    use super::settle::{Effects, PartState, Proof, Step};
    use super::settle_cli::Real;
    use std::os::unix::fs::PermissionsExt;
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("argv.log");
    let exe = dir.path().join("plangraph");
    // A stand-in for the keeper CLI: it records the argument list and answers one row per --item.
    std::fs::write(
        &exe,
        format!(
            "#!/bin/sh\necho \"$@\" >> {log}\nwhile [ $# -gt 0 ]; do\n  if [ \"$1\" = --item ]; then\n    case \"$2\" in\n      k:late) printf 'deployment-pending\\t%s\\t3h\\tdell\\n' \"$2\" ;;\n      *) printf 'closed\\t%s\\tabc\\n' \"$2\" ;;\n    esac\n  fi\n  shift\ndone\n",
            log = log.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o755)).unwrap();
    let mut fx = Real {
        repo: "x/y".into(),
        dir: dir.path().to_path_buf(),
        exe,
        train_pr: 100,
    };
    let sha = "a".repeat(40);
    let part = |items: &[&str]| PartState {
        pr: 7,
        head: sha.clone(),
        items: items.iter().map(|i| (*i).to_owned()).collect(),
        folded: true,
        writer_head: None,
        writer_record: None,
        owner: None,
        ready_at: None,
        close_items: Step::Pending,
        close_pr: Step::Pending,
        stop_writer: Step::Pending,
    };
    let proof = Proof::OwnMerge;
    assert_eq!(
        fx.close_items(&part(&["k:A", "k:B"]), &proof),
        Ok(Applied::Done)
    );
    assert!(matches!(
        fx.close_items(&part(&["k:A", "k:late"]), &proof),
        Ok(Applied::Kept(why)) if why.starts_with("k:late deployment-pending")
    ));
    assert_eq!(fx.close_items(&part(&[]), &proof), Ok(Applied::Done));
    let argv = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        argv.lines().collect::<Vec<_>>(),
        [
            "merge-close --item k:A --item k:B",
            "merge-close --item k:A --item k:late"
        ]
    );
}

#[test]
fn an_older_state_file_without_a_docs_step_loads_and_folds_once() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("s.json");
    let mut done = Settlement::new("example/repo", 9, &"d".repeat(40), vec![]);
    done.save(&path).unwrap();
    let mut raw: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    raw.as_object_mut().unwrap().remove("docs");
    std::fs::write(&path, raw.to_string()).unwrap();
    let loaded = Settlement::load(&path).unwrap().unwrap();
    assert_eq!(loaded.docs, super::settle::Step::Pending);
    done = loaded;
    assert!(!done.hold_released);
}
