use super::*;
use std::fs;

/// A fixture home with key, token and env files under the plangraph config directory.
struct Fixture {
    _dir: tempfile::TempDir,
    home: PathBuf,
    config: PathBuf,
}

fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let home = fs::canonicalize(dir.path()).unwrap().join("user");
    let config = home.join(".config/plangraph");
    fs::create_dir_all(&config).unwrap();
    for name in ["feeder.token", "client.key", "plangraph.env"] {
        fs::write(config.join(name), "fixture-value").unwrap();
    }
    fs::create_dir_all(home.join(".ssh")).unwrap();
    fs::write(home.join(".ssh/id_fixture"), "fixture-value").unwrap();
    fs::create_dir_all(home.join(".codex")).unwrap();
    fs::write(home.join(".codex/auth.json"), "fixture-value").unwrap();
    fs::create_dir_all(home.join("work")).unwrap();
    fs::write(home.join("work/notes.txt"), "plain").unwrap();
    Fixture {
        _dir: dir,
        home: home.clone(),
        config,
    }
}

fn bash(f: &Fixture, command: &str) -> Option<String> {
    check_command(command, &Context::new(&f.home, None, &f.home.join("work")))
}

#[test]
fn a_cat_of_the_config_directory_glob_is_refused_and_names_a_file() {
    let f = fixture();
    let reason = bash(&f, "cat ~/.config/plangraph/*").unwrap();
    assert!(reason.starts_with("secret-read: refused"), "{reason}");
    assert!(
        ["feeder.token", "client.key", "plangraph.env"]
            .iter()
            .any(|name| reason.contains(name)),
        "{reason}"
    );
    assert!(!reason.contains("fixture-value"), "{reason}");
}

#[test]
fn every_print_shape_of_a_key_file_is_refused() {
    let f = fixture();
    let key = f.config.join("client.key");
    for command in [
        format!("cat {}", key.display()),
        format!("head -n 1 {}", key.display()),
        format!("tail {}", key.display()),
        format!("less {}", key.display()),
        format!("sed -n p {}", key.display()),
        format!("awk 1 {}", key.display()),
        format!("grep . {}", key.display()),
        format!("grep -r secret {}", f.config.display()),
        "cat \"$HOME/.config/plangraph/client.key\"".to_owned(),
        "cat ${HOME}/.config/plangraph/client.key".to_owned(),
        "cat ../.config/plangraph/client.key".to_owned(),
        "cat ~/.config/plangraph/*.key".to_owned(),
        "cat ~/.config/*/feeder.token".to_owned(),
        "cat ~/.config/plangraph/f?eder.token".to_owned(),
        "echo start; cat ~/.config/plangraph/plangraph.env".to_owned(),
        "ls | cat ~/.config/plangraph/client.key".to_owned(),
        "true && (cat ~/.config/plangraph/client.key)".to_owned(),
        "echo $(cat ~/.config/plangraph/client.key)".to_owned(),
        "echo `cat ~/.config/plangraph/client.key`".to_owned(),
        "bash -c 'cat ~/.config/plangraph/client.key'".to_owned(),
        "sudo -n cat ~/.config/plangraph/client.key".to_owned(),
        "env FOO=1 cat ~/.config/plangraph/client.key".to_owned(),
        "cat < ~/.config/plangraph/client.key".to_owned(),
        "echo $(< ~/.config/plangraph/client.key)".to_owned(),
        "source ~/.config/plangraph/plangraph.env".to_owned(),
        ". ~/.config/plangraph/plangraph.env".to_owned(),
        "find ~/.config/plangraph -name '*.key' -exec cat {} +".to_owned(),
        "cat ~/.ssh/id_fixture".to_owned(),
        "cat ~/.codex/auth.json".to_owned(),
        "cat ~/.local/share/plangraph/keeper.json".to_owned(),
        "cat --show-all ~/.config/plangraph/client.key".to_owned(),
        "grep --file=~/.config/plangraph/client.key x".to_owned(),
        // Characters of an attached pattern are not option flags.
        "grep -ecat ~/.config/plangraph/client.key".to_owned(),
        "grep -nelq ~/.config/plangraph/client.key".to_owned(),
        "grep -mlc x ~/.config/plangraph/client.key".to_owned(),
        "grep -e -l ~/.config/plangraph/client.key".to_owned(),
        "grep -- -l ~/.config/plangraph/client.key".to_owned(),
        "grep --regexp=-l ~/.config/plangraph/client.key".to_owned(),
        "grep --regexp -c ~/.config/plangraph/client.key".to_owned(),
        "rg -ec ~/.config/plangraph/client.key".to_owned(),
        // ripgrep's -L follows symlinks and still prints matches; --count-matches is quiet only
        // for the names it lists, never for the lines of -n.
        "rg -L x ~/.config/plangraph/client.key".to_owned(),
        "rg -nL x ~/.config/plangraph/client.key".to_owned(),
    ] {
        let reason = bash(&f, &command);
        assert!(
            reason
                .as_deref()
                .is_some_and(|r| r.starts_with("secret-read: refused")),
            "{command}: {reason:?}"
        );
    }
}

#[test]
fn listing_and_measuring_a_key_file_stay_allowed() {
    let f = fixture();
    for command in [
        "ls ~/.config/plangraph",
        "ls -la ~/.config/plangraph/",
        "ls ~/.config/plangraph/*",
        "stat ~/.config/plangraph/client.key",
        "test -s ~/.config/plangraph/client.key && echo present",
        "[ -s ~/.config/plangraph/client.key ]",
        "wc -c ~/.config/plangraph/client.key",
        "wc -c < ~/.config/plangraph/client.key",
        "grep -l x ~/.config/plangraph/*",
        "grep -c x ~/.config/plangraph/client.key",
        "grep -q x ~/.config/plangraph/client.key",
        "grep -ql x ~/.config/plangraph/client.key",
        "grep -e x -l ~/.config/plangraph/client.key",
        "grep --count x ~/.config/plangraph/client.key",
        "grep -rl x ~/.config/plangraph",
        "rg -l x ~/.config/plangraph",
        "rg -c x ~/.config/plangraph/client.key",
        "rg --count-matches x ~/.config/plangraph/client.key",
        "rg --files-without-match x ~/.config/plangraph",
        "grep -L x ~/.config/plangraph/client.key",
        "ag -l x ~/.config/plangraph",
        "find ~/.config/plangraph -name '*.key'",
        "chmod 600 ~/.config/plangraph/client.key",
        "echo hello > ~/.config/plangraph/new.token",
        "cat work/notes.txt",
        "cat ~/work/notes.txt",
        "head -n 3 ~/.config/other-tool/settings",
        "sed -n 1p work/notes.txt",
        "cargo test -p pg-hook",
    ] {
        assert_eq!(bash(&f, command), None, "{command}");
    }
}

#[test]
fn the_read_tool_and_a_grep_tool_path_are_refused_on_secret_files() {
    let f = fixture();
    let ctx = Context::new(&f.home, None, &f.home.join("work"));
    let key = f.config.join("client.key");
    assert!(check_tool("Read", &json!({"file_path": key}), &ctx)
        .unwrap()
        .contains("client.key"));
    assert!(check_tool(
        "Read",
        &json!({"file_path": "~/.config/plangraph/feeder.token"}),
        &ctx
    )
    .is_some());
    assert!(check_tool("Grep", &json!({"pattern": "x", "path": f.config}), &ctx).is_some());
    assert_eq!(
        check_tool(
            "Read",
            &json!({"file_path": f.home.join("work/notes.txt")}),
            &ctx
        ),
        None
    );
    assert_eq!(
        check_tool("Glob", &json!({"pattern": "*", "path": f.config}), &ctx),
        None
    );
}

#[test]
fn a_relocated_config_home_is_guarded_too() {
    let f = fixture();
    let xdg = f.home.join("xdg");
    fs::create_dir_all(xdg.join("plangraph")).unwrap();
    fs::write(xdg.join("plangraph/reader.json"), "fixture-value").unwrap();
    let ctx = Context::new(&f.home, Some(&xdg), &f.home);
    assert!(check_command("cat $XDG_CONFIG_HOME/plangraph/reader.json", &ctx).is_some());
    assert!(check_command(&format!("cat {}/plangraph/*", xdg.display()), &ctx).is_some());
}

#[test]
fn the_hook_reads_the_pre_tool_use_payload_and_answers_two_on_a_refusal() {
    let f = fixture();
    let payload = |tool: &str, input: serde_json::Value| {
        json!({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": input,
               "cwd": f.home.join("work")})
        .to_string()
    };
    let run = |text: String| {
        let mut diagnostic = Vec::new();
        let code = run_with(text.as_bytes(), &mut diagnostic, &f.home, None);
        (code, String::from_utf8(diagnostic).unwrap())
    };
    let (code, message) = run(payload(
        "Bash",
        json!({"command": "cat ~/.config/plangraph/*"}),
    ));
    assert_eq!(code, 2);
    assert!(message.contains("secret-read: refused"), "{message}");
    assert_eq!(
        run(payload(
            "Bash",
            json!({"command": "ls ~/.config/plangraph"})
        ))
        .0,
        0
    );
    assert_eq!(
        run(payload(
            "Read",
            json!({"file_path": f.config.join("client.key")})
        ))
        .0,
        2
    );
    assert_eq!(run("not json".to_owned()).0, 0);
    assert_eq!(
        run(payload(
            "Edit",
            json!({"file_path": f.config.join("client.key")})
        ))
        .0,
        0
    );
}
