use super::fleet::*;
use super::ledger::Ledger;
use super::systemd::Out;
use std::collections::BTreeMap;
use std::sync::Mutex;

const REGISTRY: &str = r#"{"version":1,"self":"center","hosts":[
 {"id":"center","transport":"local"},
 {"id":"dell","transport":"ssh","ssh_target":"dell-ssh"},
 {"id":"desk","transport":"ssh","ssh_target":"desk-ssh","wsl_distro":"Ubuntu"},
 {"id":"odd","transport":"keeper"}]}"#;

struct Fake {
    answers: BTreeMap<String, Result<Out, String>>,
    seen: Mutex<Vec<(String, Vec<String>)>>,
}

impl Remote for Fake {
    fn run(&self, host: &FleetHost, argv: &[String]) -> Result<Out, String> {
        self.seen
            .lock()
            .unwrap()
            .push((host.id.clone(), argv.to_vec()));
        self.answers
            .get(&host.id)
            .cloned()
            .unwrap_or(Err("no answer".into()))
    }
}

fn out(code: i32, stdout: &str, stderr: &str) -> Result<Out, String> {
    Ok(Out {
        code,
        stdout: stdout.into(),
        stderr: stderr.into(),
    })
}

fn ledger_line(since: Option<i64>) -> String {
    let mut l = Ledger {
        written_at: 100,
        ..Ledger::default()
    };
    if let Some(s) = since {
        l.not_live_since.insert("unit:pg-a".into(), s);
    }
    format!("ledger\t{}\n", serde_json::to_string(&l).unwrap())
}

#[test]
fn the_registry_gives_local_ssh_wsl_and_unsupported_hosts() {
    let hosts = hosts_from_registry(REGISTRY).unwrap();
    assert_eq!(hosts[0].access, Access::Local);
    assert_eq!(
        hosts[1].access,
        Access::Ssh {
            target: "dell-ssh".into(),
            wsl_distro: None
        }
    );
    assert_eq!(
        hosts[2].access,
        Access::Ssh {
            target: "desk-ssh".into(),
            wsl_distro: Some("Ubuntu".into())
        }
    );
    assert!(matches!(&hosts[3].access, Access::Unsupported(w) if w.contains("keeper")));
}

#[test]
fn the_script_travels_base64_through_ssh_and_the_wsl_login_shell() {
    let hosts = hosts_from_registry(REGISTRY).unwrap();
    let script = remote_script(true);
    assert!(
        script.contains("host-sync --install --check --machine")
            && script.contains("cargo run --release")
    );
    let ssh = argv(&hosts[1], &script).unwrap();
    assert_eq!(
        &ssh[..6],
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "dell-ssh"
        ]
    );
    assert!(
        ssh[6].starts_with("sh -lc \"echo ") && ssh[6].ends_with("| base64 -d | sh\""),
        "{}",
        ssh[6]
    );
    let wsl = argv(&hosts[2], &script).unwrap();
    assert!(
        wsl[6].starts_with("wsl.exe --distribution Ubuntu --exec sh -lc"),
        "{}",
        wsl[6]
    );
    assert!(argv(&hosts[3], &script).is_none());
    assert_eq!(argv(&hosts[0], &script).unwrap()[..2], ["sh", "-lc"]);
}

#[test]
fn three_hosts_one_missing_an_entry_one_unreachable_one_complete() {
    let hosts = hosts_from_registry(REGISTRY).unwrap()[..3].to_vec();
    let mut answers = BTreeMap::new();
    answers.insert(
        "center".to_owned(),
        out(
            0,
            &format!(
                "nothing to do\t5 entries live\ndeploy-not-live\t0\npending\t0\n{}",
                ledger_line(None)
            ),
            "",
        ),
    );
    answers.insert(
        "dell".to_owned(),
        out(
            1,
            &format!(
                "plan\tunit\tpg-a\tpg-a.service missing\tinstall unit files and enable\nresult\tunit\tpg-a\tpg-a.service missing\tstill: not-installed\npending\t1\n{}",
                ledger_line(Some(50))
            ),
            "",
        ),
    );
    answers.insert(
        "desk".to_owned(),
        out(
            255,
            "",
            "ssh: connect to host desk-ssh port 22: Connection refused\n",
        ),
    );
    let fake = Fake {
        answers,
        seen: Mutex::new(Vec::new()),
    };
    let dir = tempfile::tempdir().unwrap();
    let (table, failed) = run_fleet(&hosts, &fake, false, dir.path());
    let text = table.join("\n");
    assert!(failed, "{text}");
    assert!(
        text.contains("center\tnothing to do\t5 entries live")
            && text.contains("center\tOK\tcomplete"),
        "{text}"
    );
    assert!(
        text.contains("dell\tresult\tunit\tpg-a\tpg-a.service missing\tstill: not-installed"),
        "{text}"
    );
    assert!(text.contains("dell\tPENDING"), "{text}");
    assert!(
        text.contains(
            "desk\tFAIL\tunreachable: ssh: connect to host desk-ssh port 22: Connection refused"
        ),
        "{text}"
    );
    assert_eq!(fake.seen.lock().unwrap().len(), 3, "every host was asked");
    assert!(dir.path().join("center.json").is_file() && dir.path().join("dell.json").is_file());
    assert!(
        !dir.path().join("desk.json").exists(),
        "no ledger row for a host that never answered"
    );
    let dell: Ledger =
        serde_json::from_slice(&std::fs::read(dir.path().join("dell.json")).unwrap()).unwrap();
    assert_eq!(dell.not_live_since["unit:pg-a"], 50);
}

#[test]
fn a_fleet_of_complete_hosts_succeeds_and_an_unsupported_transport_is_a_named_failure() {
    let hosts = hosts_from_registry(REGISTRY).unwrap();
    let mut answers = BTreeMap::new();
    for id in ["center", "dell", "desk"] {
        answers.insert(
            id.to_owned(),
            out(
                0,
                &format!(
                    "nothing to do\t3 entries live\npending\t0\n{}",
                    ledger_line(None)
                ),
                "",
            ),
        );
    }
    let fake = Fake {
        answers,
        seen: Mutex::new(Vec::new()),
    };
    let dir = tempfile::tempdir().unwrap();
    let (table, failed) = run_fleet(&hosts, &fake, true, dir.path());
    let text = table.join("\n");
    assert!(
        failed && text.contains("odd\tFAIL\ttransport keeper has no install path"),
        "{text}"
    );
    let (clean, failed) = run_fleet(&hosts[..3], &fake, true, dir.path());
    assert!(!failed, "{clean:?}");
}

#[test]
fn a_host_that_prints_no_result_is_a_failure_not_a_pass() {
    let hosts = hosts_from_registry(REGISTRY).unwrap()[..1].to_vec();
    let answers = BTreeMap::from([("center".to_owned(), out(127, "", "sh: cargo: not found\n"))]);
    let fake = Fake {
        answers,
        seen: Mutex::new(Vec::new()),
    };
    let dir = tempfile::tempdir().unwrap();
    let (table, failed) = run_fleet(&hosts, &fake, false, dir.path());
    assert!(
        failed
            && table
                .join("\n")
                .contains("install run gave no result (exit 127): sh: cargo: not found"),
        "{table:?}"
    );
}

#[test]
fn base64_matches_the_standard_vectors() {
    let hosts = hosts_from_registry(REGISTRY).unwrap();
    let encoded = |s: &str| {
        argv(&hosts[1], s).unwrap()[6]
            .split(' ')
            .nth(3)
            .unwrap()
            .to_owned()
    };
    assert_eq!(encoded("Man"), "TWFu");
    assert_eq!(encoded("Ma"), "TWE=");
    assert_eq!(encoded("M"), "TQ==");
}
