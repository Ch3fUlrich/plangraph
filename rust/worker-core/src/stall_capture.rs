//! The bounded tail of a supervised child's combined output.
//!
//! Without redaction the capture is a plain ring of the last bytes. With it, a
//! secret is scrubbed before the capture limit can drop context in front of it:
//! the buffer is redacted before every front drain, the unfinished end of the
//! buffer (a token or a PEM block still arriving) stays raw until it completes,
//! and a PEM private-key block the drain had to cut through is swallowed up to
//! its footer, so no continuation of it survives as a fragment.

/// The wrapper's end marker, on a line of its own (`"\n"` + [`crate::check::END_MARKER`]).
pub(super) const END_LINE: &[u8] = b"\nPG-CHECK-DONE:";
const HEADER: &[u8] = b"-----BEGIN ";
const FOOTER: &[u8] = b"-----END ";
const KEY: &[u8] = b"PRIVATE KEY-----";
/// Raw bytes kept at the end of a mid-stream redaction pass: a token split
/// across two reads completes inside this window.
const MARGIN: usize = 512;
/// How much of the previous read is rescanned for a footer split across reads.
const CARRY: usize = 64;

pub(super) struct Capture {
    buf: Vec<u8>,
    max: usize,
    redact: bool,
    swallowing: bool,
    carry: Vec<u8>,
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn rfind(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).rposition(|w| w == needle)
}

/// Start of the last PEM private-key header that has no footer after it.
fn open_key_start(buf: &[u8]) -> Option<usize> {
    let at = rfind(buf, HEADER)?;
    let rest = &buf[at..];
    let line = &rest[..rest.len().min(HEADER.len() + 48)];
    find(line, KEY)?;
    find(rest, FOOTER).is_none().then_some(at)
}

/// End of the first footer after `from` (just past its `PRIVATE KEY-----`).
fn footer_end_after(buf: &[u8], from: usize) -> Option<usize> {
    let foot = from + find(&buf[from..], FOOTER)?;
    let key = foot + find(&buf[foot..], KEY)?;
    Some(key + KEY.len())
}

/// End of the complete PEM private-key block that contains position `at`, if any.
fn key_block_end_containing(buf: &[u8], at: usize) -> Option<usize> {
    let head = rfind(&buf[..at.min(buf.len())], HEADER)?;
    let line = &buf[head..buf.len().min(head + HEADER.len() + 48)];
    find(line, KEY)?;
    footer_end_after(buf, head).filter(|end| *end > at)
}

/// Widen `split` so a complete PEM private-key block under the redaction split or
/// under the front cut is scrubbed whole, never cut in two by either.
fn outside_key_block(buf: &[u8], split: usize, cut: usize) -> usize {
    [split, cut]
        .into_iter()
        .filter_map(|at| key_block_end_containing(buf, at))
        .fold(split, usize::max)
}

/// Start of the wrapper's end-marker line when it is the buffer's last line.
fn marker_start(buf: &[u8]) -> Option<usize> {
    let at = rfind(buf, END_LINE)?;
    let rest = &buf[at + END_LINE.len()..];
    let digits = rest
        .iter()
        .take_while(|b| b.is_ascii_digit() || **b == b'-')
        .count();
    (digits > 0 && rest[digits..].iter().all(u8::is_ascii_whitespace)).then_some(at)
}

/// Where a mid-stream redaction pass may stop: `MARGIN` bytes before the end,
/// moved back to whitespace and to a character boundary.
fn margin_split(buf: &[u8]) -> usize {
    let mut at = buf.len().saturating_sub(MARGIN);
    while at > 0 && !buf[at - 1].is_ascii_whitespace() && buf.len() - at < 2 * MARGIN {
        at -= 1;
    }
    while at > 0 && at < buf.len() && (buf[at] & 0xC0) == 0x80 {
        at -= 1;
    }
    at
}

impl Capture {
    pub(super) fn new(max: usize, redact: bool) -> Self {
        Capture {
            buf: Vec::new(),
            max,
            redact,
            swallowing: false,
            carry: Vec::new(),
        }
    }

    pub(super) fn push(&mut self, data: &[u8]) {
        let mut data = data;
        if self.swallowing {
            match self.footer_end(data) {
                Some(at) => {
                    self.swallowing = false;
                    data = &data[at..];
                }
                None => return,
            }
        }
        self.buf.extend_from_slice(data);
        if self.buf.len() > self.max.saturating_mul(2) {
            self.make_room(false);
        }
    }

    pub(super) fn finish(mut self) -> Vec<u8> {
        if self.buf.len() > self.max {
            self.make_room(true);
        }
        self.buf
    }

    /// Index in `data` just after a PEM footer that ends the swallowed block.
    fn footer_end(&mut self, data: &[u8]) -> Option<usize> {
        let mut seen = std::mem::take(&mut self.carry);
        let skip = seen.len();
        seen.extend_from_slice(data);
        let mut from = 0;
        // The wrapper's end marker ends the swallow too: it proves the command is over
        // and must survive an unterminated block.
        let marker = find(&seen, END_LINE);
        let mut footer = None;
        while let Some(rel) = find(&seen[from..], KEY) {
            let at = from + rel;
            let before = &seen[at.saturating_sub(FOOTER.len() + 32)..at];
            if find(before, FOOTER).is_some() {
                footer = Some(at + KEY.len());
                break;
            }
            from = at + 1;
        }
        match (footer, marker) {
            (Some(end), m) if m.is_none_or(|m| end <= m) => return Some(end.saturating_sub(skip)),
            (_, Some(at)) => {
                // The part of the marker an earlier read carried over belongs to the output.
                if at < skip {
                    self.buf.extend_from_slice(&seen[at..skip]);
                }
                return Some(at.saturating_sub(skip));
            }
            _ => {}
        }
        self.carry = seen[seen.len().saturating_sub(CARRY)..].to_vec();
        None
    }

    /// Shrink the buffer to `max` bytes, scrubbing first when redacting. The wrapper's
    /// end marker line, if the buffer ends with it, is set aside raw and put back after:
    /// it carries no secret and an unterminated block must not redact it away.
    fn make_room(&mut self, closing: bool) {
        let suffix = match self.redact.then(|| marker_start(&self.buf)).flatten() {
            Some(at) => self.buf.split_off(at),
            None => Vec::new(),
        };
        let max = self.max.saturating_sub(suffix.len());
        if self.redact {
            let open = open_key_start(&self.buf);
            let split = match (closing, open) {
                (true, _) => self.buf.len(),
                (false, Some(at)) => at,
                (false, None) => outside_key_block(
                    &self.buf,
                    margin_split(&self.buf),
                    self.buf.len().saturating_sub(max),
                ),
            };
            let valid = match std::str::from_utf8(&self.buf[..split]) {
                Err(e) if e.error_len().is_none() => e.valid_up_to(),
                _ => split,
            };
            let mut out =
                crate::redact::redact(&String::from_utf8_lossy(&self.buf[..valid])).into_bytes();
            let open_at = open.filter(|_| !closing).map(|_| out.len());
            out.extend_from_slice(&self.buf[valid..]);
            self.buf = out;
            if open_at.is_some_and(|at| self.buf.len().saturating_sub(max) > at) {
                let at = open_at.unwrap_or(0);
                // The cut passes the open header: drop the whole block and
                // everything that follows of it, until its footer or the end marker.
                self.buf.truncate(at);
                self.swallowing = true;
                self.carry.clear();
            }
        }
        let cut = self.buf.len().saturating_sub(max);
        self.buf.drain(..cut);
        self.buf.extend_from_slice(&suffix);
    }
}
