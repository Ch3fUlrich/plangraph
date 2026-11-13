use std::io;

#[cfg(windows)]
pub(super) fn wide(path: &std::path::Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str().encode_wide().chain(Some(0)).collect()
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) enum Step {
    Posix,
    Replace,
    Legacy,
    Move,
}

pub(super) fn rename_with_fallback(mut call: impl FnMut(Step) -> io::Result<()>) -> io::Result<()> {
    // Refused classes/flags do not mutate either name, including replacement.
    // Sharing/access/cross-volume failures must never trigger weaker steps.
    for step in [Step::Posix, Step::Replace, Step::Legacy] {
        if let Err(e) = call(step) {
            if !refused(&e) {
                return Err(e);
            }
        } else {
            return Ok(());
        }
    }
    call(Step::Move)
}

pub(super) fn refused(error: &io::Error) -> bool {
    matches!(error.raw_os_error(), Some(87 | 1 | 50))
}

pub(super) fn buffer_size(offset: usize, name_units: usize) -> io::Result<u32> {
    name_units
        .checked_add(1)
        .and_then(|n| n.checked_mul(2))
        .and_then(|n| offset.checked_add(n))
        .and_then(|n| u32::try_from(n).ok())
        .ok_or_else(|| io::ErrorKind::InvalidInput.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refusal_fallback_order_and_stopping_are_platform_independent() {
        let steps = [Step::Posix, Step::Replace, Step::Legacy, Step::Move];
        for code in [87, 1, 50] {
            for success_at in 0..=4 {
                let mut seen = Vec::new();
                let result = rename_with_fallback(|step| {
                    seen.push(step);
                    if seen.len() - 1 == success_at {
                        Ok(())
                    } else {
                        Err(io::Error::from_raw_os_error(code))
                    }
                });
                assert_eq!(seen, steps[..(success_at + 1).min(4)]);
                assert_eq!(result.is_ok(), success_at < 4);
                if let Err(error) = result {
                    assert_eq!(error.raw_os_error(), Some(code));
                }
            }
        }
    }

    #[test]
    fn existing_destinations_use_every_refusal_fallback() {
        for code in [87, 1, 50] {
            let mut seen = Vec::new();
            rename_with_fallback(|step| {
                seen.push(step);
                if step == Step::Move {
                    Ok(())
                } else {
                    Err(io::Error::from_raw_os_error(code))
                }
            })
            .unwrap();
            assert_eq!(seen, [Step::Posix, Step::Replace, Step::Legacy, Step::Move]);
        }
    }

    #[test]
    fn real_failures_and_existing_readers_never_use_legacy_replacement() {
        for code in [5, 32, 17] {
            let mut seen = Vec::new();
            let error = rename_with_fallback(|step| {
                seen.push(step);
                Err(io::Error::from_raw_os_error(code))
            })
            .unwrap_err();
            assert_eq!(seen, [Step::Posix]);
            assert_eq!(error.raw_os_error(), Some(code));
        }
        for refused in 0..4 {
            for code in [5, 32, 17] {
                let mut calls = 0;
                let error = rename_with_fallback(|_| {
                    let failure = if calls < refused { 87 } else { code };
                    calls += 1;
                    Err(io::Error::from_raw_os_error(failure))
                })
                .unwrap_err();
                assert_eq!(calls, refused + 1);
                assert_eq!(error.raw_os_error(), Some(code));
            }
        }
    }

    #[test]
    fn rename_buffer_uses_field_offset_not_struct_tail_padding() {
        for offset in [12, 20] {
            for name in ["a", "status.json", "status-\u{1f980}.json"] {
                let units = name.encode_utf16().count();
                assert_eq!(
                    buffer_size(offset, units).unwrap() as usize,
                    offset + units * 2 + 2
                );
            }
        }
        assert!(buffer_size(20, usize::MAX).is_err());
        assert!(buffer_size(usize::MAX, 1).is_err());
        assert!(buffer_size(20, u32::MAX as usize).is_err());
    }
}
