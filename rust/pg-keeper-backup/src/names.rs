//! Dump file names: `keeper-schema-seq<SEQ>-<YYYYMMDDTHHMMZ>.dump`.

use std::time::{SystemTime, UNIX_EPOCH};

/// D-5796: no restore, import or migration may load keeper state older than this seq.
pub const MIN_SEQ: u64 = 19096;
const PREFIX: &str = "keeper-schema-seq";
const SUFFIX: &str = ".dump";

/// `YYYYMMDDTHHMMZ` for a unix time (UTC, civil-from-days).
pub fn stamp(unix_secs: u64) -> String {
    let days = (unix_secs / 86_400) as i64;
    let rem = unix_secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}{month:02}{day:02}T{:02}{:02}Z",
        rem / 3_600,
        rem % 3_600 / 60
    )
}

pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

pub fn dump_name(seq: u64, unix_secs: u64) -> String {
    format!("{PREFIX}{seq}-{}{SUFFIX}", stamp(unix_secs))
}

/// `(seq, stamp)` of a dump file name, or `None` for any other name.
pub fn parse(name: &str) -> Option<(u64, &str)> {
    let rest = name.strip_prefix(PREFIX)?.strip_suffix(SUFFIX)?;
    let (seq, stamp) = rest.split_once('-')?;
    let b = stamp.as_bytes();
    let shape = b.len() == 14
        && b[8] == b'T'
        && b[13] == b'Z'
        && b[..8].iter().chain(&b[9..13]).all(u8::is_ascii_digit);
    if !shape || seq.is_empty() || !seq.bytes().all(|c| c.is_ascii_digit()) {
        return None;
    }
    Some((seq.parse().ok()?, stamp))
}
