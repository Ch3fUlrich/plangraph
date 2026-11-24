use super::sandbox::{Launcher, Sandbox, LOCAL, REMOTE, SID};
use serde_json::json;
use std::{
    fs,
    process::Command,
    time::{Duration, UNIX_EPOCH},
};

#[path = "typed_fix_respawn.rs"]
mod typed_fix_respawn;

const OLD: &str = "work/status/handoff-old.md";
const NEW: &str = "work/status/handoff-new.md";

fn sandbox(launcher: &Launcher) -> Sandbox {
    let s = Sandbox::new(launcher);
    fs::create_dir(s.path("work/status")).unwrap();
    for (name, age, time) in [(OLD, 1, "04:00"), (NEW, 2, "04:01")] {
        fs::write(s.path(name), "# Fixture handoff\n").unwrap();
        let card = s.path(name).with_extension("card.md");
        fs::write(
            &card,
            format!("# review card · review-0001 · 2026-11-22T{time}Z\n## Drill-down\n"),
        )
        .unwrap();
        fs::File::options()
            .write(true)
            .open(card)
            .unwrap()
            .set_modified(UNIX_EPOCH + Duration::from_secs(age))
            .unwrap();
    }
    s.write_json(
        "state/launch/review.json",
        json!({
            "name": "review", "cwd": s.path("work"), "card": "status/handoff-old.card.md",
            "handoff": "status/handoff-old.md", "host": LOCAL, "model": "saved-model",
            "effort": "medium", "ctx_guard_source": "operator",
            "ctx_guard": {"handoff": 550000, "hard": 600000}, "launched_caps": {"handoff": 550000, "hard": 600000}
        }),
    );
    s
}

fn launch(s: &Sandbox) -> Command {
    let mut command = s.command();
    command
        .arg("review")
        .arg(s.path("work"))
        .args(["requested-model", "high"])
        .arg(s.path("prompt"))
        .arg("--respawn");
    command
}

pub fn check(launcher: &Launcher) {
    let s = sandbox(launcher);
    let supplied = format!(
        "Restart from handoff `{}`. Use --ctx-guard 550000,600000.\nOld brief text.\n",
        s.path(NEW).display()
    );
    fs::write(s.path("prompt"), supplied).unwrap();
    let out = launch(&s).output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    let child = s.json("child.json");
    let argv = child["argv"].as_array().unwrap();
    let prompt = argv.last().unwrap().as_str().unwrap();
    assert_eq!(
        prompt.matches(&s.path(NEW).display().to_string()).count(),
        1,
        "{prompt}"
    );
    assert!(prompt.ends_with("Old brief text.\n"), "{prompt}");
    assert!(!prompt.contains("handoff-old"), "{prompt}");
    assert!(
        prompt.contains("Model: requested-model") && prompt.contains("Effort: high"),
        "{prompt}"
    );
    assert!(
        prompt.contains("550000") && prompt.contains("600000"),
        "{prompt}"
    );
    assert!(argv
        .windows(2)
        .any(|pair| pair == ["--model", "requested-model"]));
    assert!(argv.windows(2).any(|pair| pair == ["--effort", "high"]));
    let saved = s.json("state/launch/review.json");
    assert_eq!(saved["model"], "requested-model");
    assert_eq!(saved["effort"], "high");
    assert_eq!(
        saved["launched_caps"],
        json!({"handoff": 550000, "hard": 600000})
    );
    assert_eq!(saved["handoff"], json!(s.path(NEW)));

    for (prompt, found, expected) in [
        (
            "Restart from handoff $NEW.\nThen read [handoff]($OLD).",
            "handoff-old.md",
            "handoff-new.md",
        ),
        ("Caps: handoff 700k, hard 800k.", "800000", "600000"),
        ("Live guard 700k/800k.", "800000", "600000"),
        ("Keep the compact window above 850000.", "850000", "650000"),
    ] {
        let s = sandbox(launcher);
        let prompt = prompt
            .replace("$NEW", &s.path(NEW).to_string_lossy())
            .replace("$OLD", &s.path(OLD).to_string_lossy());
        fs::write(s.path("prompt"), prompt).unwrap();
        for dry in [true, false] {
            let mut command = launch(&s);
            if dry {
                command.arg("--dry-run");
            }
            let out = command.output().unwrap();
            let stderr = s.normalize(&out.stderr);
            assert_eq!(out.status.code(), Some(2), "{stderr}");
            assert!(
                stderr.contains("PROMPT-STALE")
                    && stderr.contains(found)
                    && stderr.contains(expected),
                "{stderr}"
            );
            assert!(out.stdout.is_empty());
            assert!(!s.path("child.json").exists());
            assert!(!s.path("state/session-seq").exists());
            assert!(!s.path("state/fleet-launch-fallback.jsonl").exists());
        }
    }
    let s = sandbox(launcher);
    for handoff in [OLD, NEW] {
        fs::remove_file(s.path(handoff).with_extension("card.md")).unwrap();
    }
    let out = launch(&s).output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(s.normalize(&out.stderr).contains("PROMPT-STALE"));
    assert!(!s.path("child.json").exists());
    check_ready_card(launcher);
    check_forward(launcher);
    check_policy_caps(launcher);
    check_explicit_guard(launcher);
    check_card_order(launcher);
    check_unrelated_prose(launcher);
    typed_fix_respawn::check(launcher);
    println!("fleet-launch restart prompt checks passed");
}

fn check_ready_card(launcher: &Launcher) {
    let s = sandbox(launcher);
    let mut saved = s.json("state/launch/review.json");
    saved["session_id"] = json!(SID);
    s.write_json("state/launch/review.json", saved);
    let card = s.path("handoffs/current.card.md");
    let handoff = s.path("handoffs/current-notes.md");
    fs::create_dir(card.parent().unwrap()).unwrap();
    fs::write(&handoff, "# Fixture handoff\n").unwrap();
    fs::write(&card, "# review card · review-0001 · 2026-11-22T05:00Z\n").unwrap();
    s.write_json(
        &format!("guard/{SID}.ready.json"),
        json!({"card": card, "handoff": handoff}),
    );
    let out = launch(&s)
        .env("AUTOOS_CTX_STATE", s.path("guard"))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    let child = s.json("child.json");
    let prompt = child["argv"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_str()
        .unwrap();
    assert!(prompt.starts_with(&format!("Restart from handoff {}.", handoff.display())));
    assert_eq!(s.json("state/launch/review.json")["card"], json!(card));
}

fn check_forward(launcher: &Launcher) {
    let s = sandbox(launcher);
    s.hosts(json!([{"name": REMOTE, "ssh": "fixture-hop", "memory_floor_gib": 0}]));
    fs::write(s.path("memory-fixture-hop"), "MemAvailable: 1024 kB\n").unwrap();
    fs::write(s.path("ssh.stdout"), "forwarded\n").unwrap();
    let out = launch(&s).args(["--host", REMOTE]).output().unwrap();
    s.expect(&out, 0, "forwarded\n", "");
    let wire = s.json("ssh.json");
    let payload = &wire["payload"];
    let prompt = payload["prompt"].as_str().unwrap();
    assert!(prompt.starts_with(&format!("Restart from handoff {}.", s.path(NEW).display())));
    assert!(
        prompt.contains("550000") && prompt.contains("600000"),
        "{prompt}"
    );
    let argv = payload["argv"].as_array().unwrap();
    assert_eq!(
        (&argv[2], &argv[3]),
        (&json!("requested-model"), &json!("high"))
    );
    assert!(argv
        .windows(2)
        .any(|pair| pair == ["--ctx-guard", "550000,600000"]));
    assert_eq!(payload["spec"]["ctx_guard_source"], "operator");
    assert_eq!(
        payload["spec"]["ctx_guard"],
        json!({"handoff": 550000, "hard": 600000})
    );
    let out = launch(&s)
        .args(["--host", REMOTE, "--ctx-guard", "425000,500000"])
        .output()
        .unwrap();
    s.expect(&out, 0, "forwarded\n", "");
    let wire = s.json("ssh.json");
    let payload = &wire["payload"];
    assert!(payload["prompt"]
        .as_str()
        .unwrap()
        .contains("Caps: handoff 425000, hard 500000."));
    let argv = payload["argv"].as_array().unwrap();
    let mut guards = argv.windows(2).filter(|pair| pair[0] == "--ctx-guard");
    assert_eq!(guards.next().unwrap()[1], "425000,500000");
    assert!(guards.next().is_none());

    fs::remove_file(s.path("ssh.json")).unwrap();
    fs::write(
        s.path("prompt"),
        format!("Restart from handoff {}.", s.path(OLD).display()),
    )
    .unwrap();
    let out = launch(&s).args(["--host", REMOTE]).output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert!(s.normalize(&out.stderr).contains("PROMPT-STALE"));
    assert!(!s.path("ssh.json").exists());
}

fn check_explicit_guard(launcher: &Launcher) {
    let s = sandbox(launcher);
    fs::write(s.path("prompt"), "Caps: handoff 425000, hard 500000.").unwrap();
    let out = launch(&s)
        .args(["--ctx-guard", "425000,500000"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    let child = s.json("child.json");
    let prompt = child["argv"]
        .as_array()
        .unwrap()
        .last()
        .unwrap()
        .as_str()
        .unwrap();
    assert!(
        prompt.contains("Caps: handoff 425000, hard 500000."),
        "{prompt}"
    );
    let saved = s.json("state/launch/review.json");
    assert_eq!(
        saved["launched_caps"],
        json!({"handoff": 425000, "hard": 500000})
    );
    assert_eq!(saved["ctx_guard"], saved["launched_caps"]);

    let s = sandbox(launcher);
    fs::write(s.path("prompt"), "Caps: handoff 550000, hard 600000.").unwrap();
    let out = launch(&s)
        .args(["--ctx-guard", "425000,500000"])
        .output()
        .unwrap();
    let error = s.normalize(&out.stderr);
    assert_eq!(out.status.code(), Some(2), "{error}");
    assert!(
        error.contains("PROMPT-STALE caps 550000,600000; expected 425000,500000"),
        "{error}"
    );
    assert!(!s.path("child.json").exists());
}

fn check_card_order(launcher: &Launcher) {
    for old_mtime in [3, 4] {
        let s = sandbox(launcher);
        for (handoff, mtime) in [(OLD, old_mtime), (NEW, 3)] {
            fs::File::options()
                .write(true)
                .open(s.path(handoff).with_extension("card.md"))
                .unwrap()
                .set_modified(UNIX_EPOCH + Duration::from_secs(mtime))
                .unwrap();
        }
        let out = launch(&s).output().unwrap();
        assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
        let saved = s.json("state/launch/review.json");
        assert_eq!(saved["handoff"], json!(s.path(NEW)));
    }
}

fn check_unrelated_prose(launcher: &Launcher) {
    let s = sandbox(launcher);
    fs::write(
        s.path("prompt"),
        "The model has a context window 200000. Difficulty hard: 12. See docs/handoffs/plan.md.",
    )
    .unwrap();
    let out = launch(&s).output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert!(s.path("child.json").exists());
}

fn check_policy_caps(launcher: &Launcher) {
    for forwarded in [true, false] {
        let s = sandbox(launcher);
        let mut record = s.json("state/launch/review.json");
        record["ctx_guard_source"] = json!("role");
        s.write_json("state/launch/review.json", record);
        let mut policy = s.json("router/ops/fleet-launch-policy.json");
        policy["caps"]["roles"]["review"] = json!({"handoff": 450000, "hard": 510000});
        s.write_json("router/ops/fleet-launch-policy.json", policy);
        let mut command = launch(&s);
        if forwarded {
            s.hosts(json!([{"name": REMOTE, "ssh": "fixture-hop", "memory_floor_gib": 0}]));
            fs::write(s.path("memory-fixture-hop"), "MemAvailable: 1024 kB\n").unwrap();
            fs::write(s.path("ssh.stdout"), "forwarded\n").unwrap();
            command.args(["--host", REMOTE]);
        }
        let out = command.output().unwrap();
        assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
        if forwarded {
            let wire = s.json("ssh.json");
            let payload = &wire["payload"];
            assert!(!payload["argv"]
                .as_array()
                .unwrap()
                .iter()
                .any(|arg| arg == "--ctx-guard"));
            assert!(payload["prompt"]
                .as_str()
                .unwrap()
                .contains("Caps: handoff 450000, hard 510000."));
        } else {
            let record = s.json("state/launch/review.json");
            assert_eq!(
                record["launched_caps"],
                json!({"handoff": 450000, "hard": 510000})
            );
            assert!(record["ctx_guard_source"].is_null());
        }
    }
}
