//! Keep executable fixture writes on the shared atomic publication path.
use std::path::Path;

#[test]
fn executable_fixtures_use_the_shared_writer() {
    let rust = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let mut directories = vec![rust.to_path_buf()];
    let mut offenders = Vec::new();
    while let Some(directory) = directories.pop() {
        for entry in std::fs::read_dir(directory).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if entry.file_type().unwrap().is_dir() {
                if entry.file_name() != "target" {
                    directories.push(path);
                }
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                let name = path
                    .strip_prefix(rust)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                if name == "worker-core/tests/executable_fixtures.rs" {
                    continue;
                }
                let source = std::fs::read_to_string(&path).unwrap();
                let lines: Vec<&str> = source.lines().collect();
                for (number, line) in lines.iter().enumerate() {
                    if line.trim_start().starts_with("//") {
                        continue;
                    }
                    let literal =
                        writes_executable_mode(line) && !mode_is_not_a_fixture_write(&name, line);
                    let computed =
                        sets_computed_mode(line) && !computed_mode_is_not_a_fixture(&name, line);
                    let copied = copies_a_file(line)
                        && !copy_is_not_an_executable(&name, &statement(&lines[number..]));
                    if literal || computed || copied {
                        offenders.push(format!("{name}:{}", number + 1));
                    }
                }
            }
        }
    }
    offenders.sort();
    assert!(
        offenders.is_empty(),
        "use executable_test_support for {}",
        offenders.join(", ")
    );
}

fn mode_is_not_a_fixture_write(path: &str, line: &str) -> bool {
    match path {
        // Production file writers and directory permissions.
        "plangraph-cli/src/atomic.rs" => {
            line.trim() == "file.set_permissions(fs::Permissions::from_mode(0o755))"
        }
        "plangraph-cli/src/host_sync_release.rs" => {
            line.trim() == ".set_permissions(fs::Permissions::from_mode(0o755))?;"
        }
        "pg-spawnd/src/session/storage.rs" => line.trim() == ".mode(0o700)",
        "plangraph-cli/src/keeper_outbox/mod.rs"
        | "worker-core/src/run/record_io.rs"
        | "worker-core/src/writer_blob.rs" => line.trim() == "builder.mode(0o700);",
        // This test keeps the write descriptor open to trigger the busy-exec retry.
        "worker-core/src/stall.rs" => line.contains("set_permissions(&p,"),
        // These tests exercise mode metadata or directory access.
        "worker-core/src/sandbox_env.rs" => {
            line.contains("set_permissions(&dir,")
                || line.contains("set_permissions(&target,")
                || line.contains("set_permissions(path,")
                || line.trim() == "b.mode(0o700);"
        }
        "worker-core/src/finalize_promote_tests.rs" => line.contains("set_permissions(&dir,"),
        "worker-core/src/private_targets_tests.rs" => {
            line.contains("set_permissions(&public,")
                || line.contains("DirBuilder::new().mode(0o700).create(")
        }
        "worker-core/src/slots/liveness/tests.rs" => line.contains("set_permissions(&parent,"),
        "worker-core/src/slots/liveness/tests/lock_tests.rs" => {
            line.contains("set_permissions(&fd_dir,") || line.contains("set_permissions(&parent,")
        }
        "worker-core/src/unit_refs/tests.rs" => line.contains("set_permissions(&trees,"),
        "worker-core/src/writer_salvage_snapshot_tests.rs" => {
            line.contains("set_permissions(root.join(name),")
        }
        "pg-spawnd/src/bin/ci-temporal.rs" => line.contains("builder.permissions("),
        "pg-spawnd/src/cli_review_tests.rs" | "plangraph-cli/src/land/build_delta_tests.rs" => {
            line.contains("set_permissions(path,")
        }
        "pg-spawnd/tests/common/mod.rs" => {
            line.contains(".permissions(std::fs::Permissions::from_mode(")
                || line.contains("set_permissions(dir.path(),")
                || line.contains("DirBuilder::new().mode(0o700).create(")
        }
        "pg-spawnd/src/transport_unix.rs" => {
            line.contains(".permissions(std::fs::Permissions::from_mode(")
                || line.trim() == ".mode(0o700)"
                || line.contains("DirBuilder::new().mode(0o700).create(")
                || line.contains("set_permissions(base,")
                || line.contains("set_permissions(&dir,")
        }
        "pg-spawnd/tests/native_evidence.rs" => line.contains("directory.chmod("),
        "worker-core/src/fleet_foundation_tick_state_tests.rs" => {
            line.contains(r#"set_permissions(".","#)
        }
        "worker-core/src/reap_journal.rs" => {
            line.contains(".permissions(fs::Permissions::from_mode(")
        }
        _ => false,
    }
}

/// What a mode setter is given: a literal that is the whole argument, or anything else.
enum Argument {
    Literal(u32),
    Computed,
}

/// Every mode setter on the line with its argument. A literal counts only when it is the entire
/// argument, so `0o600 | 0o100` and an argument that continues on the next line are computed.
fn mode_arguments(line: &str) -> Vec<Argument> {
    let mut found = Vec::new();
    for setter in ["::from_mode", ".set_mode", ".chmod", ".mode"] {
        for (offset, _) in line.match_indices(setter) {
            let Some(argument) = line[offset + setter.len()..]
                .trim_start()
                .strip_prefix('(')
                .map(str::trim_start)
            else {
                continue;
            };
            if argument.starts_with(')') {
                continue; // an accessor such as `.mode()`
            }
            found.push(match literal_argument(argument) {
                Some((mode, rest))
                    if matches!(rest.trim_start().chars().next(), Some(')' | ',')) =>
                {
                    Argument::Literal(mode)
                }
                _ => Argument::Computed,
            });
        }
    }
    found
}

/// The numeric literal that starts `text` and what follows it (an integer suffix is skipped).
fn literal_argument(text: &str) -> Option<(u32, &str)> {
    let (radix, digits) = if let Some(digits) = text.strip_prefix("0o") {
        (8, digits)
    } else if let Some(digits) = text.strip_prefix("0x") {
        (16, digits)
    } else if let Some(digits) = text.strip_prefix("0b") {
        (2, digits)
    } else if text.starts_with(|c: char| c.is_ascii_digit()) {
        (10, text)
    } else {
        return None;
    };
    let end = digits
        .find(|ch: char| !ch.is_digit(radix) && ch != '_')
        .unwrap_or(digits.len());
    let mode = u32::from_str_radix(&digits[..end].replace('_', ""), radix).ok()?;
    let rest = &digits[end..];
    let rest = match rest.strip_prefix(['u', 'i']) {
        Some(width) => width.trim_start_matches(|c: char| c.is_ascii_digit()),
        None => rest,
    };
    Some((mode, rest))
}

/// A mode setter whose argument is not a plain literal: the guard cannot read its bits, so each such
/// call needs a justified exemption below.
fn sets_computed_mode(line: &str) -> bool {
    mode_arguments(line)
        .iter()
        .any(|argument| matches!(argument, Argument::Computed))
}

fn computed_mode_is_not_a_fixture(path: &str, line: &str) -> bool {
    match path {
        // The shared writer publishes with the mode its caller names.
        "worker-core/src/executable_test_support.rs" => {
            line.contains("Permissions::from_mode(mode)")
        }
        // Production writer of a remote step's declared file mode.
        "worker-core/src/remote_steps.rs" => line.contains("spec.mode"),
        // These tests walk non-executable subjects (a credential file, a socket directory) through modes
        // to see them refused.
        "og-memory/src/credentials.rs" => line.contains("Permissions::from_mode(mode)"),
        "pg-spawnd/src/transport_unix.rs" => {
            line.contains("set_permissions(&dir, std::fs::Permissions::from_mode(mode))")
        }
        "pg-spawnd/tests/serve_cli.rs" => {
            line.contains("set_permissions(tmp.path(), std::fs::Permissions::from_mode(mode))")
        }
        _ => false,
    }
}

/// `fs::copy` publishes a second copy of a file, mode included, without the shared writer's atomic rename.
fn copies_a_file(line: &str) -> bool {
    !line.trim_start().starts_with("//") && line.contains("fs::copy(")
}

/// The statement that starts at the first line, up to its `;`.
fn statement(lines: &[&str]) -> String {
    let end = lines.iter().position(|l| l.contains(';')).unwrap_or(0);
    lines[..=end.min(lines.len() - 1)].join(" ")
}

/// Copies that are production code or data no test executes. A copied executable belongs on
/// `write_executable(path, fs::read(source))`; a data exemption names the copied file, so the executable
/// copy next to it is still refused.
fn copy_is_not_an_executable(path: &str, statement: &str) -> bool {
    let data = |name: &str| statement.contains(name);
    match path {
        // Production installers and mirrors.
        "pg-hook/src/commit_msg.rs" | "pg-ctx/src/mirror_backend.rs" => true,
        "pg-hook/tests/status_files.rs" => data(".gitignore"),
        "pg-keeper-backup/tests/units.rs" => data(".timer"),
        "pg-spawnd/tests/toolchain_pin.rs" => data("rust-toolchain.toml"),
        "pg-spawnd/src/cli_review_tests.rs" => data("record.json"),
        // A Python module the hook runs through the interpreter.
        "pg-hook/tests/commit_message.rs" => data("ci_commit_gate.py"),
        // A hook file the test only compares byte for byte.
        "plangraph-cli/tests/landing_hosts.rs" => data("pre-push"),
        _ => false,
    }
}

fn writes_executable_mode(line: &str) -> bool {
    mode_arguments(line)
        .iter()
        .any(|argument| matches!(argument, Argument::Literal(mode) if mode & 0o111 != 0))
}

#[test]
fn recognizes_mode_setters_with_any_execute_bit() {
    for setter in [
        "OpenOptions::new().mode(0o711)",
        "Permissions::from_mode(0o100)",
        "permissions.set_mode(0o010)",
        "path.chmod(0o001)",
        "OpenOptions::new().mode(0x1ff)",
    ] {
        assert!(writes_executable_mode(setter), "{setter}");
    }
    for other in [
        "OpenOptions::new().mode(0o644)",
        "metadata.permissions().mode() & 0o111",
        "Permissions::from_mode(0o600)",
    ] {
        assert!(!writes_executable_mode(other), "{other}");
    }
}

#[test]
fn recognizes_computed_mode_setters() {
    for setter in [
        "std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();",
        "file.set_permissions(Permissions::from_mode(MODE))",
        "permissions.set_mode(current | extra)",
        "OpenOptions::new().mode(spec.mode)",
        "path.chmod(bits)",
    ] {
        assert!(sets_computed_mode(setter), "{setter}");
    }
    for other in [
        "OpenOptions::new().mode(0o644)",
        "metadata.permissions().mode() & 0o111",
        "Permissions::from_mode(0o600)",
        "let mode = configured_mode(role);",
    ] {
        assert!(!sets_computed_mode(other), "{other}");
    }
}

#[test]
fn recognizes_file_copies() {
    for copy in [
        "fs::copy(env!(\"CARGO_BIN_EXE_tool\"), &tool).unwrap();",
        "    std::fs::copy(",
    ] {
        assert!(copies_a_file(copy), "{copy}");
    }
    for other in [
        "fs::copy_dir(a, b)",
        "let copy = 1;",
        "// fs::copy is banned",
    ] {
        assert!(!copies_a_file(other), "{other}");
    }
}

#[test]
fn a_literal_inside_an_expression_is_a_computed_mode() {
    for setter in [
        "OpenOptions::new().mode(0o600 | 0o100)",
        "Permissions::from_mode(0o644 | 0o111)",
        "permissions.set_mode(0o600 + 0o100)",
        "OpenOptions::new().mode(",
    ] {
        assert!(sets_computed_mode(setter), "{setter}");
        assert!(!writes_executable_mode(setter), "{setter}");
    }
    assert!(writes_executable_mode("Permissions::from_mode(0o755u32)"));
    assert!(writes_executable_mode("Permissions::from_mode(0o755_u32)"));
    assert!(!sets_computed_mode("Permissions::from_mode(0o755)"));
}

#[test]
fn a_data_copy_exemption_does_not_cover_an_executable_copy_in_the_same_file() {
    let path = "pg-hook/tests/commit_message.rs";
    let executable =
        "std::fs::copy(env!(\"CARGO_BIN_EXE_commit-msg\"), hooks.join(\"commit-msg\")).unwrap();";
    let data = statement(&[
        "    std::fs::copy(",
        "        Path::new(env!(\"CARGO_MANIFEST_DIR\")).join(\"../../tools/ci_commit_gate.py\"),",
        "        tools.join(\"ci_commit_gate.py\"),",
        "    )",
        "    .unwrap();",
    ]);
    assert!(!copy_is_not_an_executable(path, executable));
    assert!(copy_is_not_an_executable(path, &data));
}
