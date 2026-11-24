use super::sandbox::{Launcher, Sandbox};
use serde::Deserialize;
use serde_json::json;
use std::fs;

pub(super) const ROUTER_CORE_APPEND: &str = "\n\nInvoke the skill router-core at startup (skills/router-core/SKILL.md in the plangraph checkout). Follow its Batch mode procedure for multi-item router work.";

#[derive(Deserialize)]
struct Case {
    name: String,
    role: String,
    session_role: String,
    flags: Vec<String>,
    window: u32,
    caps: String,
    deny_question: bool,
}

pub fn check(launcher: &Launcher) {
    let cases: Vec<Case> =
        serde_json::from_str(include_str!("../fixtures/fleet-launch/dry-run.json")).unwrap();
    for case in cases {
        eprintln!("checking {} preview", case.name);
        let s = Sandbox::new(launcher);
        let settings = format!("$ROOT/state/launch-settings/caps-{}.json", case.caps);
        let expected = include_str!("../fixtures/fleet-launch/dry-run.txt")
            .replace("$WINDOW", &case.window.to_string())
            .replace("$SETTINGS", &settings)
            .replace("$ROLE", &case.session_role)
            .replace(
                "$DENY_QUESTION",
                if case.deny_question {
                    ",AskUserQuestion"
                } else {
                    ""
                },
            );
        // Only the Rust launcher appends the router-core startup instruction.
        let expected = if case.role == "routing" && matches!(launcher, Launcher::Rust(_)) {
            let style = fs::read_to_string(s.path("router/ops/terse-style.txt")).unwrap();
            let length =
                style.trim_end_matches('\n').chars().count() + ROUTER_CORE_APPEND.chars().count();
            expected.replace("<881 chars>", &format!("<{length} chars>"))
        } else {
            expected
        };
        let stderr = format!(
            "fleet-launch: admission allowed\nfleet-launch: dry run: {settings} is written by the real launch (lean profile + both compact variables = {})\n",
            case.window
        );
        let out = s
            .launch(&case.role)
            .args(case.flags)
            .arg("--dry-run")
            .output()
            .unwrap();
        s.expect(&out, 0, &expected, &stderr);
        // The effective permission mode of the production launch path is pinned: fleet seats are
        // attended operator sessions that run in bypassPermissions by decision (they use keeper
        // tokens), and nothing wider is ever added. Their print path is guarded by
        // `pg-hook secret-read`, not by a sandbox.
        let text = String::from_utf8_lossy(&out.stdout);
        assert_eq!(
            text.matches("--permission-mode bypassPermissions").count(),
            1,
            "{} must launch with the one pinned permission mode",
            case.name
        );
        assert_eq!(
            text.matches("--permission-mode").count(),
            1,
            "{}",
            case.name
        );
        for wider in ["danger-full-access", "--dangerously-skip-permissions"] {
            assert!(!text.contains(wider), "{} launches with {wider}", case.name);
        }
        s.no_launch();
    }
    if matches!(launcher, Launcher::Pinned(_) | Launcher::Rust(_)) {
        check_pinned_profile(launcher);
    }
}

fn check_pinned_profile(launcher: &Launcher) {
    let s = Sandbox::new(launcher);
    let profile = "router/ops/fleet-settings.json";
    let mut settings = s.json(profile);
    settings["env"]["AUTOOS_CTX_HANDOFF"] = json!("110000");
    settings["env"]["AUTOOS_CTX_HARD"] = json!("170000");
    for key in [
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS",
    ] {
        settings["env"][key] = json!("400000");
    }
    s.write_json(profile, settings);
    let out = s
        .launch("review")
        .args(["--ctx-guard", "110000,170000", "--dry-run"])
        .output()
        .unwrap();
    let expected = include_str!("../fixtures/fleet-launch/dry-run.txt")
        .replace("$WINDOW", "400000")
        .replace("$SETTINGS", "$ROOT/router/ops/fleet-settings.json")
        .replace("$ROLE", "review")
        .replace("$DENY_QUESTION", ",AskUserQuestion");
    s.expect(&out, 0, &expected, "fleet-launch: admission allowed\n");
    s.no_launch();
}
