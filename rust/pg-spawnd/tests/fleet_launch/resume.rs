//! A respawn of a numbered worker session continues the saved transcript under the same name.
use super::sandbox::{Launcher, Sandbox, SID};
use serde_json::json;

fn saved(s: &Sandbox, name: &str) {
    s.write_json(
        &format!("state/launch/sessions/{name}.json"),
        json!({"name":"writer","session_name":name,"session_id":SID,"cwd":s.path("work"),
               "prompt_file":s.path("prompt"),"children":[],"seq":776}),
    );
}

fn dry_line(s: &Sandbox, name: &str, extra: &[&str]) -> (i32, String, String) {
    let out = s
        .launch(name)
        .args(["--respawn", "--dry-run"])
        .args(extra)
        .output()
        .unwrap();
    (
        out.status.code().unwrap(),
        s.normalize(&out.stdout),
        s.normalize(&out.stderr),
    )
}

pub fn check(launcher: &Launcher) {
    // Worker role: `--resume <saved session_id>` and the saved `-n`/`--remote-control` name.
    let s = Sandbox::new(launcher);
    saved(&s, "writer-0776");
    let (code, stdout, stderr) = dry_line(&s, "writer-0776", &[]);
    assert_eq!(code, 0, "{stderr}");
    for want in [
        format!("--bg --resume {SID} "),
        "--remote-control writer-0776 -n writer-0776 ".to_owned(),
    ] {
        assert!(stdout.contains(&want), "{stdout} lacks {want}");
    }
    // The saved name travels through `--session` as well.
    let (_, stdout, _) = dry_line(&s, "writer-0000", &["--session", "writer-0776"]);
    assert!(stdout.contains(&format!("--resume {SID}")), "{stdout}");
    assert!(stdout.contains("-n writer-0776 "), "{stdout}");

    // A non-worker role starts a fresh session.
    let s = Sandbox::new(launcher);
    s.write_json(
        "state/launch/sessions/review-0001.json",
        json!({"name":"review","session_id":SID,"cwd":s.path("work"),"children":[]}),
    );
    let (code, stdout, stderr) = dry_line(&s, "review-0001", &[]);
    assert_eq!(code, 0, "{stderr}");
    assert!(!stdout.contains("--resume"), "{stdout}");

    // A numbered worker whose transcript cannot be resumed is refused with the reason, never
    // started as a fresh session under the old name.
    let s = Sandbox::new(launcher);
    let (code, stdout, stderr) = dry_line(&s, "writer-0777", &[]);
    assert_eq!((code, stdout.as_str()), (2, ""), "{stderr}");
    assert_eq!(
        stderr,
        "fleet-launch: respawn refused: writer-0777 has no saved session_id to resume\n"
    );
    for (id, reason) in [
        (json!("--dangerous"), "is not a plain token"),
        (json!(""), "has no saved session_id"),
        (json!(7), "has no saved session_id"),
    ] {
        s.write_json(
            "state/launch/sessions/writer-0778.json",
            json!({"name":"writer","session_id":id,"cwd":s.path("work"),"children":[]}),
        );
        let (code, stdout, stderr) = dry_line(&s, "writer-0778", &[]);
        assert_eq!((code, stdout.as_str()), (2, ""), "{stderr}");
        assert!(
            stderr.contains("respawn refused") && stderr.contains(reason),
            "{stderr}"
        );
    }
}
