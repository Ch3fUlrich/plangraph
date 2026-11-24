//! The findings producer and its hand-off to the resume reader.

mod common;

use common::*;
use pg_session_resume::deliver::deliver;
use pg_session_resume::engine::{resume_findings, stop_done};
use serde_json::json;
use std::process::Command;

#[test]
fn deliver_numbers_rounds_and_writes_the_sidecar_with_the_text_untouched() {
    let dir = tempfile::tempdir().unwrap();
    let text = "REQUEST_CHANGES\n  HIGH a.rs:1 x\n\n";
    let first = deliver(dir.path(), "lane-a", HEAD, text).unwrap();
    assert_eq!(first.file_name().unwrap(), "lane-a.fix-round-1.txt");
    assert_eq!(std::fs::read_to_string(&first).unwrap(), text);
    assert_eq!(
        std::fs::read_to_string(dir.path().join("lane-a.fix-round-1.head")).unwrap(),
        HEAD
    );
    let second = deliver(dir.path(), "lane-a", &HEAD2.to_uppercase(), "again").unwrap();
    assert_eq!(second.file_name().unwrap(), "lane-a.fix-round-2.txt");
    assert_eq!(
        std::fs::read_to_string(dir.path().join("lane-a.fix-round-2.head")).unwrap(),
        HEAD2
    );
    assert!(std::fs::read_dir(dir.path())
        .unwrap()
        .flatten()
        .all(|e| !e.file_name().to_string_lossy().ends_with(".tmp")));
}

#[test]
fn deliver_refuses_bad_input_and_writes_nothing() {
    let dir = tempfile::tempdir().unwrap();
    for (lane, head, text) in [
        ("lane-a", "abc", "x"),
        ("lane-a", HEAD, "  \n"),
        ("../up", HEAD, "x"),
        (".hid", HEAD, "x"),
        ("", HEAD, "x"),
    ] {
        assert!(
            deliver(dir.path(), lane, head, text).is_err(),
            "{lane} {head} {text:?}"
        );
    }
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

#[test]
fn a_delivered_round_resumes_the_stopped_session_with_its_text() {
    let r = rig("writer", json!({}));
    done(&r, HEAD);
    let mut h = Fake::new(5000);
    stop_done(&mut h, &r.cfg, 60, None);
    deliver(&r.cfg.pr_dir, "lane-a", HEAD, "the review says: fix it\n").unwrap();
    resume_findings(&mut h, &r.cfg);
    assert_eq!(h.runs.last().unwrap().1[4], "the review says: fix it\n");
    assert_eq!(record(&r, "writer")["resume"]["round"], 1);
}

#[test]
fn the_deliver_command_reads_stdin_and_prints_the_file() {
    let dir = tempfile::tempdir().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_pg-session-resume"))
        .args(["deliver", "--lane", "lane-a", "--head", HEAD, "--pr-dir"])
        .arg(dir.path())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    std::io::Write::write_all(child.stdin.as_mut().unwrap(), b"from stdin").unwrap();
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success());
    let printed = String::from_utf8(out.stdout).unwrap();
    assert!(
        printed.trim().ends_with("lane-a.fix-round-1.txt"),
        "{printed}"
    );
    assert_eq!(
        std::fs::read_to_string(printed.trim()).unwrap(),
        "from stdin"
    );
}

#[test]
fn concurrent_producers_never_share_a_round_or_mix_a_head_with_another_text() {
    let dir = std::sync::Arc::new(tempfile::tempdir().unwrap());
    let start = std::sync::Arc::new(std::sync::Barrier::new(8));
    let threads: Vec<_> = (0..8)
        .map(|i| {
            let (dir, start) = (dir.clone(), start.clone());
            std::thread::spawn(move || {
                let head = format!("{i:040x}");
                start.wait();
                deliver(dir.path(), "lane-a", &head, &format!("review {i}")).unwrap();
                (head, format!("review {i}"))
            })
        })
        .collect();
    let sent: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    for round in 1..=8u64 {
        let head =
            std::fs::read_to_string(dir.path().join(format!("lane-a.fix-round-{round}.head")))
                .unwrap();
        let text =
            std::fs::read_to_string(dir.path().join(format!("lane-a.fix-round-{round}.txt")))
                .unwrap();
        assert!(
            sent.contains(&(head, text)),
            "round {round}: head and text come from one review"
        );
    }
    assert!(!dir.path().join("lane-a.fix-round-9.txt").exists());
}
