use super::*;
use std::{cell::RefCell, io::Read};

const OLD: &[u8] = br#"{"wake_calls":1,"other":"retained"}"#;
const NEW: &[u8] = br#"{"wake_calls":2,"other":"retained"}"#;

type Outcome = (io::Result<()>, Vec<Stage>, Vec<(Stage, Option<i32>)>);

fn exercise(failures: &[(Stage, i32)], replacing: bool) -> Outcome {
    exercise_with_reader(failures, replacing, true)
}

fn exercise_with_reader(
    failures: &[(Stage, i32)],
    replacing: bool,
    retain_reader: bool,
) -> Outcome {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("status-\u{1f980}.json");
    if replacing {
        fs::write(&path, OLD).unwrap();
        assert!(!fs::metadata(&path).unwrap().permissions().readonly());
    }
    let mut held = (replacing && (retain_reader || cfg!(not(windows))))
        .then(|| fs::File::open(&path).unwrap());
    let seen = RefCell::new(Vec::new());
    let errors = RefCell::new(Vec::new());
    let result = atomic_write(
        &path,
        NEW,
        &mut Calls {
            before: |stage| {
                seen.borrow_mut().push(stage);
                match failures.iter().find(|(failure, _)| *failure == stage) {
                    Some((_, code)) => Err(io::Error::from_raw_os_error(*code)),
                    None => Ok(()),
                }
            },
            report: |stage, error: &io::Error| {
                errors.borrow_mut().push((stage, error.raw_os_error()))
            },
        },
    );
    if result.is_ok() {
        assert_eq!(fs::read(&path).unwrap(), NEW);
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&fs::read(&path).unwrap()).unwrap()
                ["wake_calls"],
            2
        );
    } else if replacing {
        assert_eq!(fs::read(&path).unwrap(), OLD);
    } else {
        assert!(!path.exists());
    }
    if let Some(held) = &mut held {
        let mut bytes = Vec::new();
        held.read_to_end(&mut bytes).unwrap();
        assert_eq!(bytes, OLD, "fallback changed the held snapshot");
    }
    let entries: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    let expected: Vec<_> = if result.is_ok() || replacing {
        vec![path.file_name().unwrap().to_owned()]
    } else {
        vec![]
    };
    assert_eq!(entries, expected, "temporary name leaked");
    (result, seen.into_inner(), errors.into_inner())
}

#[test]
fn error_87_at_every_prepublication_site_uses_a_real_io_fallback() {
    for replacing in [false, true] {
        for (site, fallback) in [
            (Stage::Absolute, Stage::CanonicalParent),
            (Stage::CreateDelete, Stage::CreatePlain),
            (Stage::Write, Stage::WritePath),
            (Stage::Metadata, Stage::MetadataOpen),
            (Stage::Sync, Stage::SyncReopened),
        ] {
            // Plain creation selects MoveFileExW, which cannot replace an open
            // destination even when the reader shares DELETE. Other preparation
            // fallbacks still reach POSIX rename and retain the snapshot handle.
            let (result, seen, errors) =
                exercise_with_reader(&[(site, 87)], replacing, site != Stage::CreateDelete);
            result.unwrap();
            assert!(seen.contains(&fallback), "{site:?}: {seen:?}");
            assert_eq!(errors, [(site, Some(87))]);
            if site == Stage::CreateDelete {
                assert!(!seen.contains(&Stage::Posix));
                assert_eq!(seen.last(), Some(&Stage::Move));
            }
        }
    }
    let (result, seen, errors) = exercise(&[(Stage::Permissions, 87)], true);
    result.unwrap();
    assert!(seen.contains(&Stage::PermissionsPath));
    assert_eq!(errors, [(Stage::Permissions, Some(87))]);
}

#[test]
fn each_refused_rename_class_publishes_first_and_existing_documents() {
    let stages = [Stage::Posix, Stage::Replace, Stage::Legacy, Stage::Move];
    for replacing in [false, true] {
        for code in [87, 1, 50] {
            for refused in 1..=4 {
                let failures: Vec<_> = stages[..refused].iter().map(|&s| (s, code)).collect();
                // Refusing POSIX semantics tests closed-destination publication,
                // not the separate held-reader contract that only POSIX supplies.
                let (result, seen, errors) = exercise_with_reader(&failures, replacing, false);
                assert_eq!(result.is_ok(), refused < 4);
                assert_eq!(
                    &seen[seen.len() - (refused + 1).min(4)..],
                    &stages[..(refused + 1).min(4)]
                );
                assert_eq!(
                    errors,
                    failures
                        .iter()
                        .map(|&(s, c)| (s, Some(c)))
                        .collect::<Vec<_>>()
                );
                if let Err(error) = result {
                    assert_eq!(error.raw_os_error(), Some(code));
                }
            }
        }
    }
}

#[test]
fn fallback_failures_are_named_and_never_publish_unsynced_or_unpermissioned_bytes() {
    for (first, fallback) in [
        (Stage::Absolute, Stage::CanonicalParent),
        (Stage::CreateDelete, Stage::CreatePlain),
        (Stage::Write, Stage::WriteOpen),
        (Stage::Write, Stage::WritePath),
        (Stage::Metadata, Stage::MetadataOpen),
        (Stage::Metadata, Stage::MetadataHandle),
        (Stage::Permissions, Stage::PermissionsPath),
        (Stage::Sync, Stage::SyncOpen),
        (Stage::Sync, Stage::SyncReopened),
    ] {
        for code in [87, 5, 32] {
            let (result, seen, errors) = exercise(&[(first, 87), (fallback, code)], true);
            assert_eq!(result.unwrap_err().raw_os_error(), Some(code));
            assert_eq!(seen.last(), Some(&fallback));
            assert_eq!(errors, [(first, Some(87)), (fallback, Some(code))]);
        }
    }
}

#[test]
fn access_sharing_and_cross_volume_failures_stop_at_the_actual_site() {
    for site in [
        Stage::Absolute,
        Stage::CreateDelete,
        Stage::Write,
        Stage::Metadata,
        Stage::Permissions,
        Stage::Sync,
        Stage::Posix,
    ] {
        for code in [5, 32, 17] {
            let (result, seen, errors) = exercise(&[(site, code)], true);
            assert_eq!(result.unwrap_err().raw_os_error(), Some(code));
            assert_eq!(seen.last(), Some(&site));
            assert_eq!(errors, [(site, Some(code))]);
        }
    }
}

#[test]
fn successful_first_publication_has_no_failure_diagnostic() {
    let (result, _, errors) = exercise(&[], false);
    result.unwrap();
    assert!(errors.is_empty(), "{errors:?}");
}

#[test]
fn combined_error_87_sites_recover_without_skipping_durability_or_permissions() {
    for replacing in [false, true] {
        let mut failures = vec![
            (Stage::Absolute, 87),
            (Stage::CreateDelete, 87),
            (Stage::Write, 87),
            (Stage::Metadata, 87),
            (Stage::Sync, 87),
        ];
        if replacing {
            failures.push((Stage::Permissions, 87));
        }
        // CreateDelete refusal selects the non-POSIX MoveFileExW path.
        let (result, seen, errors) = exercise_with_reader(&failures, replacing, false);
        result.unwrap();
        assert!(seen.contains(&Stage::SyncReopened));
        assert_eq!(seen.last(), Some(&Stage::Move));
        if replacing {
            assert!(seen.contains(&Stage::PermissionsPath));
        }
        for (stage, code) in failures {
            assert!(errors.contains(&(stage, Some(code))), "{errors:?}");
        }
    }
}

#[test]
fn fallback_preserves_existing_permissions() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("status.json");
    fs::write(&path, OLD).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    }
    let permissions = fs::metadata(&path).unwrap().permissions();
    let mut errors = Vec::new();
    atomic_write(
        &path,
        NEW,
        &mut Calls {
            before: |stage| {
                if stage == Stage::Permissions {
                    Err(io::Error::from_raw_os_error(87))
                } else {
                    Ok(())
                }
            },
            report: |stage, error: &io::Error| errors.push((stage, error.raw_os_error())),
        },
    )
    .unwrap();
    assert_eq!(errors, [(Stage::Permissions, Some(87))]);
    assert_eq!(fs::metadata(&path).unwrap().permissions(), permissions);
    assert_eq!(fs::read(path).unwrap(), NEW);
}

#[cfg(windows)]
#[test]
fn refused_ex_class_still_reaches_sharing_failure_and_preserves_destination() {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::{
        Foundation::ERROR_ACCESS_DENIED,
        Storage::FileSystem::{FILE_SHARE_READ, FILE_SHARE_WRITE},
    };
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("status.json");
    fs::write(&path, OLD).unwrap();
    assert!(!fs::metadata(&path).unwrap().permissions().readonly());
    let mut held = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .open(&path)
        .unwrap();
    let mut failures = Vec::new();
    let error = atomic_write(
        &path,
        NEW,
        &mut Calls {
            before: |stage| {
                if matches!(stage, Stage::Posix | Stage::Replace) {
                    Err(io::Error::from_raw_os_error(87))
                } else {
                    Ok(())
                }
            },
            report: |stage, error: &io::Error| failures.push((stage, error.raw_os_error())),
        },
    )
    .unwrap_err();
    // Legacy rename rejects any open replacement destination with access denied;
    // it need not open it for DELETE first and report a sharing violation (32).
    assert_eq!(error.raw_os_error(), Some(ERROR_ACCESS_DENIED as i32));
    assert_eq!(
        failures,
        [
            (Stage::Posix, Some(87)),
            (Stage::Replace, Some(87)),
            (Stage::Legacy, Some(ERROR_ACCESS_DENIED as i32))
        ]
    );
    let mut snapshot = Vec::new();
    held.read_to_end(&mut snapshot).unwrap();
    assert_eq!(snapshot, OLD);
    assert_eq!(fs::read(&path).unwrap(), OLD);
    let entries: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    assert_eq!(entries, ["status.json"]);
}

#[cfg(windows)]
#[test]
fn non_posix_fallbacks_reject_even_delete_sharing_readers_without_publication() {
    use windows_sys::Win32::Foundation::ERROR_ACCESS_DENIED;
    for (refusals, stopped) in [
        (vec![Stage::Posix], Stage::Replace),
        (vec![Stage::Posix, Stage::Replace], Stage::Legacy),
        (
            vec![Stage::Posix, Stage::Replace, Stage::Legacy],
            Stage::Move,
        ),
        (vec![Stage::CreateDelete], Stage::Move),
    ] {
        let failures: Vec<_> = refusals.iter().map(|&stage| (stage, 87)).collect();
        // Rust File::open shares READ | WRITE | DELETE. Keep it open: a refusal
        // must not relax Windows' non-POSIX destination-handle restriction.
        let (result, seen, errors) = exercise(&failures, true);
        assert_eq!(
            result.unwrap_err().raw_os_error(),
            Some(ERROR_ACCESS_DENIED as i32)
        );
        assert_eq!(seen.last(), Some(&stopped));
        let mut expected: Vec<_> = failures.iter().map(|&(s, c)| (s, Some(c))).collect();
        expected.push((stopped, Some(ERROR_ACCESS_DENIED as i32)));
        assert_eq!(errors, expected);
    }
}
