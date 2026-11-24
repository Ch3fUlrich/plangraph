use super::*;

fn host(route: Option<&str>, distro: Option<&str>) -> Candidate {
    Candidate {
        name: "worker-a".into(),
        route: route.map(Into::into),
        wsl_distro: distro.map(Into::into),
        cwd: "/srv/it's".into(),
        mem_floor_mb: 0,
    }
}
fn words(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}
fn args(command: &Command) -> Vec<String> {
    command
        .get_args()
        .map(|a| a.to_string_lossy().into_owned())
        .collect()
}

#[test]
fn ssh_run_uses_the_bounded_route_and_quotes_one_remote_shell_line() {
    let command = remote_command(
        &host(Some("worker-a"), None),
        &words(&["cargo", "test x"]),
        true,
    )
    .unwrap();
    assert_eq!(
        args(&command),
        [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "--",
            "worker-a",
            "cd '/srv/it'\\''s' && 'cargo' 'test x'"
        ]
    );
}

#[test]
fn a_route_that_could_pass_an_option_is_refused() {
    for route in ["-oProxyCommand=x", "a b", ""] {
        let refused = remote_command(&host(Some(route), None), &words(&["true"]), true);
        assert!(refused.is_err(), "{route}");
    }
}

#[test]
fn a_wsl_host_runs_through_wsl_exec_in_its_checkout() {
    let mut place = host(Some("worker-a"), Some("Ubuntu-24.04"));
    place.cwd = "/srv/plain".into();
    let command = remote_command(&place, &words(&["pg-spawnd", "probe"]), true).unwrap();
    assert_eq!(
        args(&command)[5..],
        [
            "worker-a",
            "wsl.exe",
            "--distribution",
            "Ubuntu-24.04",
            "--cd",
            "/srv/plain",
            "--exec",
            "pg-spawnd",
            "probe"
        ]
    );
    let probe = remote_command(
        &host(Some("worker-a"), Some("Ubuntu")),
        &words(&["x"]),
        false,
    )
    .unwrap();
    assert!(!args(&probe).contains(&"--cd".to_string()));
}

#[test]
fn an_invalid_wsl_distribution_is_refused() {
    for distro in ["-bad", "a b", "x;y", ""] {
        assert!(
            remote_command(&host(Some("worker-a"), Some(distro)), &words(&["x"]), true).is_err()
        );
    }
}

#[test]
fn a_local_host_has_no_remote_command() {
    assert!(remote_command(&host(None, None), &words(&["x"]), true).is_err());
}

#[test]
fn the_verdict_message_says_the_busy_retry_was_exhausted() {
    let message = verdict_error(Verdict::HostBusy, 1);
    assert!(
        message.contains("host_busy") && message.contains("retry"),
        "{message}"
    );
    assert!(!message.contains("every eligible host"), "{message}");
    assert!(verdict_error(Verdict::TestFailure, 3).contains("exit 3"));
}

#[test]
fn wsl_values_with_spaces_are_double_quoted_for_the_windows_shell() {
    let mut place = host(Some("worker-a"), Some("Ubuntu"));
    place.cwd = "/srv/my repo".into();
    let command =
        remote_command(&place, &words(&["cargo", "test", "two words", "a=b"]), true).unwrap();
    assert_eq!(
        args(&command)[5..],
        [
            "worker-a",
            "wsl.exe",
            "--distribution",
            "Ubuntu",
            "--cd",
            "\"/srv/my repo\"",
            "--exec",
            "cargo",
            "test",
            "\"two words\"",
            "a=b"
        ]
    );
}

#[test]
fn wsl_values_with_shell_metacharacters_are_refused() {
    for bad in [
        "a&b", "a|b", "a;b", "a\"b", "a'b", "a$b", "a`b", "a%b", "a^b", "a<b", "a>b", "a(b)",
        "a\nb", "",
    ] {
        let mut place = host(Some("worker-a"), Some("Ubuntu"));
        place.cwd = "/srv".into();
        assert!(
            remote_command(&place, &words(&["x", bad]), true).is_err(),
            "{bad:?} as argument"
        );
        place.cwd = bad.into();
        assert!(
            remote_command(&place, &words(&["x"]), true).is_err(),
            "{bad:?} as cwd"
        );
    }
}

#[test]
fn wsl_values_may_hold_non_ascii_letters_but_not_non_ascii_syntax() {
    let mut place = host(Some("worker-a"), Some("Ubuntu"));
    place.cwd = "/srv/dépôt 日本".into();
    let command = remote_command(&place, &words(&["echo", "é"]), true).unwrap();
    assert!(args(&command).contains(&"\"/srv/dépôt 日本\"".to_string()));
    for bad in ["a＆b", "a\u{a0}b", "a\u{2028}b"] {
        assert!(
            remote_command(&place, &words(&["x", bad]), true).is_err(),
            "{bad:?}"
        );
    }
}
