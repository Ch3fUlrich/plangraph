//! The bounded tail of a supervised child's combined output.
//!
//! Without redaction the capture is a plain ring of the last bytes. With it, a
//! secret is scrubbed before the capture limit can drop context in front of it:
//! the buffer is redacted before every front drain, the unfinished end of the
//! buffer (a token or a PEM block still arriving) stays raw until it completes,
//! and a PEM private-key block the drain had to cut through is swallowed up to
//! its footer, so no continuation of it survives as a fragment.

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
        while let Some(rel) = find(&seen[from..], KEY) {
            let at = from + rel;
            let before = &seen[at.saturating_sub(FOOTER.len() + 8)..at];
            if find(before, FOOTER).is_some() {
                return Some((at + KEY.len()).saturating_sub(skip));
            }
            from = at + 1;
        }
        self.carry = seen[seen.len().saturating_sub(CARRY)..].to_vec();
        None
    }

    /// Shrink the buffer to `max` bytes, scrubbing first when redacting.
    fn make_room(&mut self, closing: bool) {
        if self.redact {
            let open = open_key_start(&self.buf);
            let split = match (closing, open) {
                (true, _) => self.buf.len(),
                (false, Some(at)) => at,
                (false, None) => margin_split(&self.buf),
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
            if open_at.is_some_and(|at| self.buf.len().saturating_sub(self.max) > at) {
                let at = open_at.unwrap_or(0);
                // The cut passes the open header: drop the whole block and
                // everything that follows of it, until its footer.
                self.buf.truncate(at);
                self.swallowing = true;
                self.carry.clear();
            }
        }
        let cut = self.buf.len().saturating_sub(self.max);
        self.buf.drain(..cut);
    }
}
