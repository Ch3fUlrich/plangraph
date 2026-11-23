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
                    if (line.contains("0o755") || line.contains("0o700"))
                        && (line.contains("from_mode")
                            || line.contains("set_mode")
                            || line.contains(".chmod("))
                        && !mode_is_not_a_fixture_write(&name, line)
                    {
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
        // The shared fixture writer and production file writers.
        "worker-core/src/executable_test_support.rs"
        | "plangraph-cli/src/atomic.rs"
        | "plangraph-cli/src/host_sync_release.rs" => {
            line.contains("file.set_permissions(")
                || line.trim() == ".set_permissions(fs::Permissions::from_mode(0o755))?;"
        }
        // This test keeps the write descriptor open to trigger the busy-exec retry.
        "worker-core/src/stall.rs" => line.contains("set_permissions(&p,"),
        // These tests exercise mode metadata or directory access.
        "worker-core/src/sandbox_env.rs" => {
            line.contains("set_permissions(&dir,")
                || line.contains("set_permissions(&target,")
                || line.contains("set_permissions(path,")
        }
        "worker-core/src/finalize_promote_tests.rs" => line.contains("set_permissions(&dir,"),
        "worker-core/src/private_targets_tests.rs" => line.contains("set_permissions(&public,"),
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
            line.contains(".permissions(") || line.contains("set_permissions(dir.path(),")
        }
        "pg-spawnd/src/transport_unix.rs" => {
            line.contains(".permissions(")
                || line.contains("set_permissions(base,")
                || line.contains("set_permissions(&dir,")
        }
        "pg-spawnd/tests/native_evidence.rs" => line.contains("directory.chmod("),
        "worker-core/src/fleet_foundation_tick_state_tests.rs" => {
            line.contains(r#"set_permissions(".","#)
        }
        "worker-core/src/reap_journal.rs" => line.contains(".permissions("),
        _ => false,
    }
}
