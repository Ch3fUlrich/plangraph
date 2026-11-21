#![cfg(unix)]
//! `pg-worktree-sweep --check-units`: the fleet check lists a unit whose worktree path is missing.
//! Contract: only whole words that start with `/` and hold no whitespace or `; & | ( ) < >` and a
//! path-valued field's value are reported; shell text is never read for the report.
use std::{fs, path::Path, process::Command};

fn unit(units: &Path, name: &str, body: &str) {
    fs::write(
        units.join(format!("{name}.service")),
        format!("[Service]\n{body}\n"),
    )
    .unwrap();
}

fn check(config: &Path) -> (bool, String) {
    let out = Command::new(env!("CARGO_BIN_EXE_pg-worktree-sweep"))
        .arg("--check-units")
        .env("XDG_CONFIG_HOME", config)
        .output()
        .unwrap();
    (
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).into_owned(),
    )
}

#[test]
fn whole_word_paths_and_path_fields_are_reported_when_their_worktree_is_missing() {
    let tmp = tempfile::tempdir().unwrap();
    let units = tmp.path().join("systemd/user");
    let trees = tmp.path().join("app-worktrees");
    fs::create_dir_all(&units).unwrap();
    fs::create_dir_all(trees.join("alive/bin")).unwrap();
    let t = trees.display();
    unit(&units, "gone", &format!("ExecStart={t}/gone/bin/run --x"));
    unit(&units, "alive", &format!("ExecStart={t}/alive/bin/run"));
    unit(
        &units,
        "workdir",
        &format!("WorkingDirectory=\"{t}/spaced; dir\""),
    );
    fs::create_dir_all(tmp.path().join("state/runs/run-a/worktree")).unwrap();
    unit(
        &units,
        "native-ok",
        &format!(
            "WorkingDirectory={}/state/runs/run-a/worktree",
            tmp.path().display()
        ),
    );
    unit(
        &units,
        "native-gone",
        &format!(
            "WorkingDirectory={}/state/runs/run-b/worktree",
            tmp.path().display()
        ),
    );
    let (ok, text) = check(tmp.path());
    assert!(!ok);
    for listed in ["gone", "workdir", "native-gone"] {
        assert!(
            text.contains(&format!("unit-worktree-missing {listed}.service:")),
            "{text}"
        );
    }
    for spared in ["alive", "native-ok"] {
        assert!(!text.contains(&format!("{spared}.service")), "{text}");
    }
}

#[test]
fn shell_text_and_separator_words_are_never_reported() {
    let tmp = tempfile::tempdir().unwrap();
    let units = tmp.path().join("systemd/user");
    let trees = tmp.path().join("app-worktrees");
    fs::create_dir_all(&units).unwrap();
    let t = trees.display();
    // Every line names a worktree that does not exist; none is a whole word, so none is reported.
    for (name, body) in [
        ("shell", format!("ExecStart=/bin/sh -c 'exec {t}/g1/run'")),
        (
            "redirect",
            format!("ExecStart=/bin/sh -c 'cat <\"{t}/g2/config\"'"),
        ),
        ("semi", format!("ExecStart=\"{t}/g3;blue/run\"")),
        ("spaced", format!("ExecStart=\"{t}/g4; blue/run\"")),
        ("assign", format!("Environment=ROOT={t}/g5/x")),
        (
            "nice",
            format!("ExecStart=/usr/bin/nice -n5 sh -c 'ROOT={t}/g6;true'"),
        ),
    ] {
        unit(&units, name, &body);
    }
    let (ok, text) = check(tmp.path());
    assert!(ok && text.is_empty(), "{text}");
}

#[test]
fn an_unquoted_working_directory_with_spaces_is_one_path() {
    let tmp = tempfile::tempdir().unwrap();
    let units = tmp.path().join("systemd/user");
    let trees = tmp.path().join("app-worktrees");
    fs::create_dir_all(&units).unwrap();
    fs::create_dir_all(trees.join("team blue")).unwrap();
    let t = trees.display();
    unit(&units, "alive", &format!("WorkingDirectory={t}/team blue"));
    unit(&units, "gone", &format!("WorkingDirectory={t}/gone blue"));
    let (ok, text) = check(tmp.path());
    assert!(!ok);
    assert!(
        text.contains(&format!(
            "unit-worktree-missing gone.service: {t}/gone blue"
        )),
        "{text}"
    );
    assert!(!text.contains("alive.service"), "{text}");
}
