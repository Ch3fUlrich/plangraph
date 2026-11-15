//! Black-box regressions for the existing Python record/verdict boundary.
use std::{fs, path::PathBuf, process::Command};

const HEAD: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ORDER: &str = "D-5866 operator order: review pass on claude-haiku-5.5";

struct Seat {
    tmp: tempfile::TempDir,
    root: PathBuf,
    run: PathBuf,
}

impl Seat {
    fn new(reason: &str, extra: &str, served: &str) -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let run = tmp.path().join(format!("run-{}", "b".repeat(32)));
        fs::create_dir(&run).unwrap();
        fs::create_dir(tmp.path().join("router")).unwrap();
        fs::write(
            tmp.path().join("router/decisions-index.tsv"),
            "id\tsource\nD-5866\tOPERATOR\nD-5874\tROUTER\n",
        )
        .unwrap();
        fs::write(
            run.join("record.json"),
            format!(
                r#"{{
            "run_id":"{}", "role":"review", "item_key":"demo:A",
            "status":"completed", "exit_code":0,
            "completion":{{"head_sha":"{HEAD}"}},
            "served_model":"{served}", "finish_reason":"stop",
            "chosen_leg":{{"model":"claude-haiku-5-5", "family":"anthropic"}},
            "override_reason":"{reason}", "plan":{{"p_source":"override"}},
            "card_hash":"{}", "cwd":"/work", "started":1, "ended":2,
            "attempts":[{{"status":"completed"}}] {extra}
        }}"#,
                run.file_name().unwrap().to_str().unwrap(),
                "c".repeat(64)
            ),
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(run.join("record.json"), fs::Permissions::from_mode(0o600))
                .unwrap();
        }
        fs::write(run.join("live.log"), "VERDICT: REQUEST_CHANGES\n").unwrap();
        Self { tmp, root, run }
    }

    fn verdict(&self, head: &str, accepted: bool, diagnostic: &str) {
        let out = Command::new("python3")
            .current_dir(&self.root)
            .env("PG_DATA_DIR", self.tmp.path())
            .env("PYTHONPATH", self.root.join("src"))
            .args(["tools/seat_verdict.py", "--key", "demo:A", "--seat-dir"])
            .arg(&self.run)
            .args(["--writer-family", "openai", "--token-dir"])
            .arg(self.tmp.path())
            .args(["--head", head, "--dry-run"])
            .output()
            .unwrap();
        let stdout = String::from_utf8_lossy(&out.stdout);
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(out.status.success(), accepted, "{stdout}\n{stderr}");
        if accepted {
            assert!(stdout.contains("\"ok\": false"), "{stdout}");
            assert!(stdout.contains("anthropic"), "{stdout}");
        } else {
            assert!(stderr.contains(diagnostic), "{stderr}");
        }
    }

    fn discovery(&self, accepted: bool) {
        let out = Command::new("python3")
            .current_dir(&self.root)
            .env("PG_DATA_DIR", self.tmp.path())
            .env(
                "PYTHONPATH",
                std::env::join_paths([self.root.join("tools"), self.root.join("src")]).unwrap(),
            )
            .args([
                "-c",
                r#"
import sys
from seat_record import eligible_records
item = dict(key='demo:A', head_sha='a'*40, writer_families=['openai'])
seats, reasons = eligible_records(item, sys.argv[1])
assert len(seats) == int(sys.argv[2]), (seats, reasons)
if seats:
    assert seats[0]['ok'] is False and seats[0]['family'] == 'anthropic'
else:
    assert any('pinned seat' in reason for reason in reasons), reasons
"#,
            ])
            .arg(self.tmp.path())
            .arg(if accepted { "1" } else { "0" })
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
}

#[test]
fn operator_order_posts_record_verdict_and_backfills() {
    let seat = Seat::new(ORDER, "", "claude-haiku-5-5");
    seat.verdict(HEAD, true, "");
    seat.discovery(true);
}

#[test]
fn unapproved_overrides_stay_pinned() {
    for reason in [
        "please use haiku",
        "D-999999 operator order",
        "D-5874 operator order",
        "D-5866 operatorish order",
        "prefix D-5866 operator order",
    ] {
        let seat = Seat::new(reason, "", "claude-haiku-5-5");
        seat.verdict(HEAD, false, "pinned seat");
        seat.discovery(false);
    }
}

#[test]
fn operator_order_never_waives_explicit_pin_flags() {
    for extra in [
        r#", "spawn_pin":true"#,
        r#", "pin":true"#,
        r#", "pinned":true"#,
        r#", "plan":{"p_source":"override","spawn_pin":true}"#,
        r#", "chosen_leg":{"model":"claude-haiku-5-5","spawn_pin":true}"#,
    ] {
        let seat = Seat::new(ORDER, extra, "claude-haiku-5-5");
        seat.verdict(HEAD, false, "pinned seat");
        seat.discovery(false);
    }
}

#[test]
fn operator_order_keeps_model_witness_and_head_binding() {
    Seat::new(ORDER, "", "unwitnessed").verdict(HEAD, false, "unwitnessed");
    Seat::new(
        ORDER,
        r#", "chosen_leg":{"model":"opencode/mimo-v2.6-free","family":"mimo"}"#,
        "unwitnessed",
    )
    .verdict(HEAD, false, "unwitnessed");
    Seat::new(ORDER, "", "claude-haiku-5-5").verdict(&"d".repeat(40), false, "head");
}

#[test]
fn missing_malformed_and_duplicate_authority_fail_closed() {
    for index in [
        None,
        Some("D-5866\tROUTER\n"),
        Some("D-5866\toperatorish\n"),
        Some("D-5866\tOPERATOR\nD-5866\tROUTER\n"),
        Some("D-5866 OPERATOR\n"),
    ] {
        let seat = Seat::new(ORDER, "", "claude-haiku-5-5");
        let path = seat.tmp.path().join("router/decisions-index.tsv");
        match index {
            Some(text) => fs::write(path, text).unwrap(),
            None => fs::remove_file(path).unwrap(),
        }
        seat.verdict(HEAD, false, "pinned seat");
    }
}
