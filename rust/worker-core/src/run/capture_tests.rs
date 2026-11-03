//! Hermetic capture regression tests.
use super::{
    activity::is_activity,
    capture::{pump, Capture, CaptureCfg, Stream::*},
    livelog::{LiveLog, LiveLogCfg},
    scrub::Scrubber,
    tail::{Keeper, RECENT, STDOUT_HEAD, STDOUT_TAIL},
};
use crate::redact::REDACTED;
use std::{
    ffi::OsString,
    fs,
    io::{self, Read},
    path::PathBuf,
};
use tempfile::tempdir;

fn env(name: &str, value: &str) -> (OsString, OsString) {
    (name.into(), value.into())
}
fn secret() -> String {
    ["private ", "fixture ", "value"].concat()
}
fn cfg() -> LiveLogCfg {
    LiveLogCfg {
        flush_at: 64,
        max: 256,
        keep: 128,
    }
}
fn capture(path: Option<PathBuf>) -> Capture {
    Capture::new(CaptureCfg {
        live_log: path,
        live_cfg: cfg(),
        scrubber: Scrubber::from_env(&[env("MY_KEY", &secret())]),
    })
}
#[test]
fn scrub_values_and_patterns() {
    let short = ["tiny", "val"].concat();
    let long = format!("{} extended", secret());
    let multiline = ["first fixture", "second fixture"].join("\n");
    let s = Scrubber::from_env(&[
        env("key", &secret()),
        env("TOKEN", &long),
        env("auth", &short),
        env("PASSWORD", "infinity"),
        env("SECRET", &multiline),
        env("plain", "ordinary fixture"),
    ]);
    for value in [
        secret(),
        long,
        "infinity".into(),
        multiline,
        "first fixture".into(),
        "second fixture".into(),
    ] {
        assert_eq!(s.scrub(&value), REDACTED);
    }
    assert_eq!(s.scrub(&short), short);
    assert_eq!(s.scrub("ordinary fixture"), "ordinary fixture");
    for value in [
        "8080",
        "-1",
        "1.5e3",
        "true",
        "+12345678",
        "12345678.5",
        "1.2345678e3",
        "FALSE",
    ] {
        assert_eq!(Scrubber::from_env(&[env("KEY", value)]).scrub(value), value);
    }
    let bearer = ["Bear", "er ", &"c9".repeat(12)].concat();
    let once = s.scrub(&format!("{}\n{bearer}", secret()));
    assert!(!once.contains(&"c9".repeat(12)));
    assert!(once.contains(REDACTED));
    assert_eq!(s.scrub(&once), once);
}
#[test]
fn activity_table() {
    for (line, expected) in [
        ("> build · model", false),
        ("\x1b[2K\x1b[0m", false),
        ("\x1b]title\x07", false),
        ("\x1b]title\x1b\\", false),
        (" \t\n", false),
        ("hello", true),
        ("\x1b[32mok\x1b[0m", true),
        ("> x", true),
    ] {
        assert_eq!(is_activity(line), expected, "{line:?}");
    }
}
fn ignored(bytes: &[u8]) -> String {
    // Independent reference: valid input, with only edge fragments invalid.
    String::from_utf8_lossy(bytes).replace('\u{fffd}', "")
}
fn reference(text: &str) -> String {
    let b = text.as_bytes();
    if b.len() <= 65536 {
        text.into()
    } else {
        format!(
            "{}\n[... output truncated ...]\n{}",
            ignored(&b[..STDOUT_HEAD]),
            ignored(&b[b.len() - STDOUT_TAIL..])
        )
    }
}
#[test]
fn keeper_reference_boundaries_and_pieces() {
    for size in [0, 65535, 65536, 65537, 200000] {
        for character in ["é", "🦀"] {
            let mut text = "x".repeat(size);
            for offset in [16383, size.saturating_sub(49153)] {
                if offset + character.len() <= text.len()
                    && text.is_char_boundary(offset)
                    && text.is_char_boundary(offset + character.len())
                {
                    text.replace_range(offset..offset + character.len(), character);
                }
            }
            for piece in [1, 7, 8191] {
                let mut k = Keeper::new(STDOUT_HEAD, STDOUT_TAIL);
                // push accepts str: cuts retreat to a complete character.
                let mut start = 0;
                while start < text.len() {
                    let mut end = (start + piece).min(text.len());
                    while !text.is_char_boundary(end) {
                        end += 1;
                    }
                    k.push(&text[start..end]);
                    start = end;
                }
                assert_eq!(k.total(), text.len());
                assert_eq!(k.render(), reference(&text));
                assert_eq!(
                    k.recent(4096),
                    ignored(&text.as_bytes()[text.len().saturating_sub(4096)..])
                );
            }
        }
    }
    let mut k = Keeper::new(STDOUT_HEAD, STDOUT_TAIL);
    for _ in 0..640 {
        k.push(&"x".repeat(8192));
    }
    assert!(k.len_bytes() <= STDOUT_HEAD + 2 * STDOUT_TAIL + 8192);
    let mut stderr = Keeper::new(0, RECENT);
    stderr.push(&"é".repeat(5000));
    assert_eq!(stderr.recent(4096), "é".repeat(2048));
}
#[test]
fn live_batch_rotate_and_private() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("live.log");
    let mut log = LiveLog::new(path.clone(), cfg());
    assert_eq!(log.feed(&"a".repeat(63)), None);
    assert!(!path.exists());
    assert_eq!(log.feed("b"), None);
    assert_eq!(fs::read(&path).unwrap().len(), 64);
    log.feed("rest");
    assert_eq!(fs::read(&path).unwrap().len(), 64);
    assert_eq!(log.flush(), None);
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        format!("{}brest", "a".repeat(63))
    );
    log.feed(&format!("{}é", "z".repeat(191)));
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        format!("{}é", "z".repeat(126))
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    // The retained byte window begins inside a four-byte character.
    log.feed(&format!("{}🦀{}", "a".repeat(129), "b".repeat(126)));
    assert_eq!(fs::read_to_string(path).unwrap(), "b".repeat(126));
}
#[test]
fn log_errors_are_bounded_and_drained() {
    let dir = tempdir().unwrap();
    let parent = dir.path().join("file");
    fs::write(&parent, "x").unwrap();
    let c = capture(Some(parent.join("live.log")));
    for _ in 0..6 {
        c.feed(Stdout, b"\n");
        c.flush_live();
    }
    let events = c.take_events();
    assert_eq!(events.len(), 3);
    for event in &events {
        assert_eq!(event["event"], "live_log_error");
        assert!(event["error"].as_str().is_some());
        assert!(!event.to_string().contains(&secret()));
    }
    assert!(c.take_events().is_empty());
    // Repairing the path must not re-enable the writer after three failures.
    fs::remove_file(&parent).unwrap();
    fs::create_dir(&parent).unwrap();
    c.feed(Stdout, b"more\n");
    c.flush_live();
    assert!(!parent.join("live.log").exists());
}
#[test]
fn decoding_lines_finish_and_seq() {
    let c = capture(None);
    c.feed(Stdout, &[0xe2]);
    assert_eq!(c.snapshot().seq, 0);
    c.feed(Stdout, &[0x82, 0xac, b'\n', 0xff]);
    assert_eq!(c.snapshot().stdout_final, "€\n");
    c.finish(Stdout);
    assert_eq!(c.snapshot().stdout_final, "€\n\u{fffd}");
    let seq = c.snapshot().seq;
    c.finish(Stdout);
    c.feed(Stdout, b"ignored\n");
    c.flush_live();
    assert_eq!(c.snapshot().seq, seq);
    let c = capture(None);
    c.feed(Stdout, b"a\nb");
    assert_eq!(c.snapshot().stdout_final, "a\n");
    c.finish(Stdout);
    assert_eq!(c.snapshot().stdout_final, "a\nb");
    let c = capture(None);
    c.feed(Stdout, b"\xe2\x82");
    c.finish(Stdout);
    assert_eq!(c.snapshot().stdout_final, "\u{fffd}");
    let c = capture(None);
    c.feed(Stderr, b"x\r");
    assert_eq!(c.snapshot().stderr_final, "x\r");
}
#[test]
fn activity_only_when_processed() {
    let c = capture(None);
    c.feed(Stdout, "> build · model\n".as_bytes());
    assert_eq!(c.snapshot().last_activity_ts, None);
    c.feed(Stdout, b"hello");
    assert_eq!(c.snapshot().last_activity_ts, None);
    c.finish(Stdout);
    assert!(c.snapshot().last_activity_ts.unwrap() > 0.0);
    let c = capture(None);
    c.feed(Stderr, b"ok\n");
    assert!(c.snapshot().last_activity_ts.is_some());
    let c = capture(None);
    c.feed(Stdout, b"\n");
    let seq = c.snapshot().seq;
    c.feed(Stdout, b"");
    assert_eq!(c.snapshot().seq, seq);
    // A full quiet stderr tail does not move seq when the rendered tail is unchanged.
    c.feed(Stderr, &vec![b'\n'; 4096]);
    let seq = c.snapshot().seq;
    c.feed(Stderr, b"\n");
    assert_eq!(c.snapshot().seq, seq);
}
#[test]
fn secret_split_block_and_pending_cap() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("live.log");
    let c = capture(Some(path.clone()));
    let value = secret();
    c.feed(Stdout, &value.as_bytes()[..5]);
    assert!(c.snapshot().stdout_recent.is_empty());
    c.feed(Stdout, format!("{}\n", &value[5..]).as_bytes());
    c.finish(Stdout);
    assert_eq!(c.snapshot().stdout_final, format!("{REDACTED}\n"));
    assert_eq!(c.snapshot().stdout_recent, format!("{REDACTED}\n"));
    assert_eq!(fs::read_to_string(path).unwrap(), format!("{REDACTED}\n"));
    assert!(c.take_events().is_empty());
    let c = capture(None);
    c.feed(Stdout, &vec![b'x'; 65536]);
    assert_eq!(c.snapshot().seq, 0);
    c.feed(Stdout, b"x");
    assert!(!c.snapshot().stdout_final.is_empty());
    assert!(c.snapshot().last_activity_ts.is_some());
    // A multiline environment value must be scrubbed as one block.
    let value = ["first fixture", "second fixture"].join("\n");
    let c = Capture::new(CaptureCfg {
        live_log: None,
        live_cfg: cfg(),
        scrubber: Scrubber::from_env(&[env("KEY", &value)]),
    });
    c.feed(Stdout, format!("{value}\n").as_bytes());
    assert_eq!(c.snapshot().stdout_final, format!("{REDACTED}\n"));
}
#[test]
fn shared_log_order_and_threads() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("live.log");
    let c = capture(Some(path.clone()));
    c.feed(Stdout, b"first\n");
    c.feed(Stderr, b"second\n");
    c.feed(Stdout, b"third\n");
    c.flush_live();
    assert_eq!(fs::read_to_string(path).unwrap(), "first\nsecond\nthird\n");
    let c = capture(None);
    std::thread::scope(|s| {
        for stream in [Stdout, Stderr] {
            let c = &c;
            s.spawn(move || {
                for n in 0..500 {
                    c.feed(stream, format!("{n}\n").as_bytes());
                }
                c.finish(stream);
            });
        }
    });
    let expected: String = (0..500).map(|n| format!("{n}\n")).collect();
    let snap = c.snapshot();
    assert_eq!(snap.stdout_final, expected);
    assert_eq!(snap.stderr_final, expected);
    let c = capture(None);
    c.feed(Stderr, format!("{}\n", "é".repeat(5000)).as_bytes());
    assert_eq!(c.snapshot().stderr_final, format!("{}\n", "é".repeat(2047)));
}
#[test]
fn pumps_finish_after_eof_and_error() {
    let c = capture(None);
    pump(io::Cursor::new(b"a\nb"), Stdout, &c);
    assert_eq!(c.snapshot().stdout_final, "a\nb");
    struct Broken {
        sent: bool,
        interrupted: bool,
    }
    impl Read for Broken {
        fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
            if !self.interrupted {
                self.interrupted = true;
                return Err(io::ErrorKind::Interrupted.into());
            }
            if self.sent {
                return Err(io::ErrorKind::Other.into());
            }
            self.sent = true;
            b[..3].copy_from_slice(b"abc");
            Ok(3)
        }
    }
    let c = capture(None);
    pump(
        Broken {
            sent: false,
            interrupted: false,
        },
        Stderr,
        &c,
    );
    assert_eq!(c.snapshot().stderr_final, "abc");
}
