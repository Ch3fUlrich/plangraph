use std::fs;
use std::path::Path;
use std::process::{Command, Output};

fn make_exec(path: &Path) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, "#!/bin/sh\nexit 0\n").unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }
}

fn run_bin(
    bin: &str,
    home: &Path,
    path_env: &Path,
    extra_env: &[(&str, &Path)],
    args: &[&str],
) -> Output {
    let mut cmd = Command::new(bin);
    cmd.env_clear()
        .env("HOME", home)
        .env("PATH", path_env)
        .args(args);
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    cmd.output().unwrap()
}

fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).unwrap()
}

#[test]
fn protoc_check_names_missing_well_known_types() {
    let root = tempfile::tempdir().unwrap();
    for (bin, args) in [
        (env!("CARGO_BIN_EXE_host-sync"), &["--check-protoc"][..]),
        (
            env!("CARGO_BIN_EXE_plangraph"),
            &["host-sync", "--check-protoc"][..],
        ),
    ] {
        let output = run_bin(
            bin,
            root.path(),
            root.path(),
            &[("PROTOC", &root.path().join("missing-protoc"))],
            args,
        );
        assert_eq!(output.status.code(), Some(1));
        assert!(
            stdout(&output).starts_with("FAIL protoc google/protobuf/field_mask.proto:"),
            "{output:?}"
        );
    }
}

#[test]
fn correct_path_is_a_noop_even_without_data_checkout() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let path_dir = root.path().join("existing-bin");
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&path_dir.join(tool));
    }
    fs::create_dir_all(&home).unwrap();

    let out = run_bin(env!("CARGO_BIN_EXE_host-sync"), &home, &path_dir, &[], &[]);
    assert_eq!(out.status.code(), Some(0), "{}", stdout(&out));
    let text = stdout(&out);
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        assert!(text.contains(&format!("OK {tool}:")), "{text}");
    }
    assert_eq!(fs::read_dir(&home).unwrap().count(), 0);
    assert_eq!(fs::read_dir(&path_dir).unwrap().count(), 3);
}

#[test]
fn links_missing_handoff_check_from_data_checkout_and_second_run_is_noop() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = home.join(".local/bin");
    let data = root.path().join("private-data");
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&data.join("router/bin").join(tool));
    }
    // Pre-populate resume-digest and fleet-name as regular files so only handoff-check is missing.
    make_exec(&bin.join("resume-digest"));
    make_exec(&bin.join("fleet-name"));

    let out = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &bin,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(out.status.code(), Some(0), "{}", stdout(&out));
    let text = stdout(&out);
    assert!(text.contains("LINKED handoff-check:"), "{text}");
    assert!(text.contains("OK resume-digest:"), "{text}");
    assert!(text.contains("OK fleet-name:"), "{text}");
    assert!(fs::symlink_metadata(bin.join("handoff-check"))
        .unwrap()
        .file_type()
        .is_symlink());
    assert!(!fs::symlink_metadata(bin.join("resume-digest"))
        .unwrap()
        .file_type()
        .is_symlink());
    assert!(!fs::symlink_metadata(bin.join("fleet-name"))
        .unwrap()
        .file_type()
        .is_symlink());

    // Second run on the now-complete PATH is a no-op.
    let second = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &bin,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(second.status.code(), Some(0));
    let second_text = stdout(&second);
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        assert!(
            second_text.contains(&format!("OK {tool}:")),
            "{second_text}"
        );
    }
}

#[test]
fn creates_missing_local_bin_and_links_all_via_pg_data_dir() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = home.join(".local/bin");
    let data = root.path().join("custom-data");
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&data.join("router/bin").join(tool));
    }
    fs::create_dir_all(&home).unwrap();

    let out = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &bin,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(out.status.code(), Some(0), "{}", stdout(&out));
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        let link = bin.join(tool);
        assert!(fs::metadata(&link).unwrap().is_file());
        assert_eq!(
            fs::canonicalize(&link).unwrap(),
            fs::canonicalize(data.join("router/bin").join(tool)).unwrap()
        );
    }
}

#[test]
fn prints_named_fail_when_data_checkout_is_missing() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin).unwrap();

    let out = run_bin(env!("CARGO_BIN_EXE_host-sync"), &home, &bin, &[], &[]);
    assert_eq!(out.status.code(), Some(1));
    let text = stdout(&out);
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        assert!(text.contains(&format!("FAIL {tool}:")), "{text}");
    }
}

#[test]
fn prints_named_fail_for_missing_or_non_executable_source_tool() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = home.join(".local/bin");
    let data = root.path().join("private-data");
    make_exec(&data.join("router/bin/handoff-check"));
    make_exec(&data.join("router/bin/resume-digest"));
    // fleet-name is missing, and make resume-digest non-executable on unix.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            data.join("router/bin/resume-digest"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
    }

    let out = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &bin,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(out.status.code(), Some(1));
    let text = stdout(&out);
    assert!(text.contains("LINKED handoff-check:"), "{text}");
    assert!(text.contains("FAIL fleet-name:"), "{text}");
    #[cfg(unix)]
    assert!(text.contains("FAIL resume-digest:"), "{text}");
}

#[cfg(unix)]
#[test]
fn replaces_dangling_symlink_but_refuses_regular_non_executable_file() {
    use std::os::unix::fs::{symlink, PermissionsExt};

    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = home.join(".local/bin");
    let data = root.path().join("private-data");
    fs::create_dir_all(&bin).unwrap();
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&data.join("router/bin").join(tool));
    }
    symlink(
        root.path().join("gone/handoff-check"),
        bin.join("handoff-check"),
    )
    .unwrap();
    fs::write(bin.join("resume-digest"), "not-executable").unwrap();
    fs::set_permissions(bin.join("resume-digest"), fs::Permissions::from_mode(0o644)).unwrap();

    let out = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &bin,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(out.status.code(), Some(1));
    let text = stdout(&out);
    assert!(text.contains("LINKED handoff-check:"), "{text}");
    assert!(text.contains("LINKED fleet-name:"), "{text}");
    assert!(text.contains("FAIL resume-digest:"), "{text}");
    assert_eq!(
        fs::canonicalize(bin.join("handoff-check")).unwrap(),
        fs::canonicalize(data.join("router/bin/handoff-check")).unwrap()
    );
    assert_eq!(
        fs::read_to_string(bin.join("resume-digest")).unwrap(),
        "not-executable"
    );
}

#[test]
fn fails_without_writing_when_target_bin_dir_is_not_on_path() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let other_on_path = root.path().join("other-bin");
    let data = root.path().join("private-data");
    fs::create_dir_all(&other_on_path).unwrap();
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&data.join("router/bin").join(tool));
    }

    let out = run_bin(
        env!("CARGO_BIN_EXE_host-sync"),
        &home,
        &other_on_path,
        &[("PG_DATA_DIR", &data)],
        &[],
    );
    assert_eq!(out.status.code(), Some(1));
    let text = stdout(&out);
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        assert!(text.contains(&format!("FAIL {tool}:")), "{text}");
    }
    assert!(!home.join(".local/bin").exists());
}

#[test]
fn plangraph_subcommand_accepts_relative_data_dir_and_rejects_bad_flags() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home");
    let bin = root.path().join("custom-bin");
    let data = root.path().join("rel-data");
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        make_exec(&data.join("router/bin").join(tool));
    }
    fs::create_dir_all(&home).unwrap();

    let out = Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .current_dir(root.path())
        .env_clear()
        .env("HOME", &home)
        .env("PATH", &bin)
        .args([
            "host-sync",
            "--data-dir",
            "rel-data",
            "--bin-dir",
            bin.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", stdout(&out));
    for tool in ["handoff-check", "resume-digest", "fleet-name"] {
        assert!(fs::metadata(bin.join(tool)).unwrap().is_file());
    }

    let bad = run_bin(
        env!("CARGO_BIN_EXE_plangraph"),
        &home,
        &bin,
        &[],
        &["host-sync", "--unknown", "x"],
    );
    assert_eq!(bad.status.code(), Some(2));
    assert!(String::from_utf8(bad.stderr).unwrap().contains("usage:"));
}
