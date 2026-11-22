use super::sandbox::{Launcher, Sandbox};
use serde::Deserialize;

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
        s.no_launch();
    }
}
