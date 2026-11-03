//! Byte-bounded head/tail retention; UTF-8 fragments are discarded on rendering.
pub const STDOUT_HEAD: usize = 16384;
pub const STDOUT_TAIL: usize = 49152;
pub const RECENT: usize = 4096;
pub struct Keeper {
    head: Vec<u8>,
    tail: Vec<u8>,
    h: usize,
    t: usize,
    total: usize,
}
pub(crate) fn complete(bytes: &[u8]) -> &str {
    let mut start = 0;
    while start < bytes.len() && bytes[start] & 0xc0 == 0x80 {
        start += 1;
    }
    let bytes = &bytes[start..];
    match std::str::from_utf8(bytes) {
        Ok(s) => s,
        Err(e) => std::str::from_utf8(&bytes[..e.valid_up_to()]).unwrap(),
    }
}
impl Keeper {
    pub fn new(head: usize, tail: usize) -> Self {
        Self {
            head: Vec::new(),
            tail: Vec::new(),
            h: head,
            t: tail,
            total: 0,
        }
    }
    pub fn push(&mut self, text: &str) {
        let bytes = text.as_bytes();
        self.head
            .extend_from_slice(&bytes[..bytes.len().min(self.h - self.head.len())]);
        if bytes.len() >= self.t {
            self.tail.clear();
            self.tail.extend_from_slice(&bytes[bytes.len() - self.t..]);
        } else {
            let excess = (self.tail.len() + bytes.len()).saturating_sub(self.t);
            self.tail.drain(..excess);
            self.tail.extend_from_slice(bytes);
        }
        self.total += bytes.len();
    }
    pub fn render(&self) -> String {
        if self.total <= self.h {
            return complete(&self.head).to_owned();
        }
        if self.total <= self.h + self.t {
            let mut bytes = self.head.clone();
            bytes.extend_from_slice(&self.tail[self.tail.len() - (self.total - self.h)..]);
            return String::from_utf8(bytes).unwrap();
        }
        format!(
            "{}\n[... output truncated ...]\n{}",
            complete(&self.head),
            complete(&self.tail)
        )
    }
    pub fn recent(&self, n: usize) -> String {
        complete(&self.tail[self.tail.len().saturating_sub(n)..]).to_owned()
    }
    pub fn total(&self) -> usize {
        self.total
    }
    #[cfg(test)]
    pub fn len_bytes(&self) -> usize {
        self.head.len() + self.tail.len()
    }
}
