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
                for (number, line) in source.lines().enumerate() {
                    if writes_executable_mode(line) && !mode_is_not_a_fixture_write(&name, line) {
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
        "worker-core/src/idle_targets_tests.rs" => line.contains("set_permissions(&hidden,"),
        "worker-core/src/reap_journal.rs" => {
            line.contains(".permissions(fs::Permissions::from_mode(")
        }
        _ => false,
    }
}

fn writes_executable_mode(line: &str) -> bool {
    ["::from_mode", ".set_mode", ".chmod", ".mode"]
        .iter()
        .any(|setter| {
            line.match_indices(setter).any(|(offset, _)| {
                let rest = line[offset + setter.len()..].trim_start();
                let Some(literal) = rest.strip_prefix('(').map(str::trim_start) else {
                    return false;
                };
                let (radix, digits) = if let Some(digits) = literal.strip_prefix("0o") {
                    (8, digits)
                } else if let Some(digits) = literal.strip_prefix("0x") {
                    (16, digits)
                } else if let Some(digits) = literal.strip_prefix("0b") {
                    (2, digits)
                } else {
                    (10, literal)
                };
                let end = digits
                    .find(|ch: char| !ch.is_digit(radix) && ch != '_')
                    .unwrap_or(digits.len());
                u32::from_str_radix(&digits[..end].replace('_', ""), radix)
                    .is_ok_and(|mode| mode & 0o111 != 0)
            })
        })
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
