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

pub(super) fn rename_with_fallback(
    replacing: bool,
    mut call: impl FnMut(Step) -> io::Result<()>,
) -> io::Result<()> {
    // Only POSIX guarantees held snapshots AND fresh opens during replacement.
    // Weaker steps are safe only for first publication, serialized by the lock.
    for step in [Step::Posix, Step::Replace, Step::Legacy] {
        if let Err(e) = call(step) {
            if replacing || !matches!(e.raw_os_error(), Some(87 | 1 | 50)) {
                return Err(e);
            }
        } else {
            return Ok(());
        }
    }
    call(Step::Move)
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
                let result = rename_with_fallback(false, |step| {
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
    fn real_failures_and_existing_readers_never_use_legacy_replacement() {
        for (replacing, codes) in [(false, &[5, 32, 17][..]), (true, &[87, 1, 50][..])] {
            for &code in codes {
                let mut seen = Vec::new();
                let error = rename_with_fallback(replacing, |step| {
                    seen.push(step);
                    Err(io::Error::from_raw_os_error(code))
                })
                .unwrap_err();
                assert_eq!(seen, [Step::Posix]);
                assert_eq!(error.raw_os_error(), Some(code));
            }
        }
        for refused in 0..4 {
            for code in [5, 32, 17] {
                let mut calls = 0;
                let error = rename_with_fallback(false, |_| {
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
}
