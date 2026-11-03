//! Shared private appender; three IO failures disable it without failing a run.
use super::{
    record_io::{append_private, atomic_write_private},
    tail::complete,
};
use std::{
    fs,
    io::{self, Read, Seek, SeekFrom},
    path::PathBuf,
};
#[derive(Clone, Copy)]
pub struct LiveLogCfg {
    pub flush_at: usize,
    pub max: usize,
    pub keep: usize,
}
impl Default for LiveLogCfg {
    fn default() -> Self {
        Self {
            flush_at: 16384,
            max: 2 * 1024 * 1024,
            keep: 1024 * 1024,
        }
    }
}
pub struct LiveLog {
    path: PathBuf,
    cfg: LiveLogCfg,
    buf: String,
    size: Option<u64>,
    errors: usize,
}
impl LiveLog {
    pub fn new(path: PathBuf, cfg: LiveLogCfg) -> Self {
        Self {
            path,
            cfg,
            buf: String::new(),
            size: None,
            errors: 0,
        }
    }
    pub fn feed(&mut self, text: &str) -> Option<io::ErrorKind> {
        if self.errors >= 3 {
            return None;
        }
        self.buf.push_str(text);
        if self.buf.len() >= self.cfg.flush_at {
            self.flush()
        } else {
            None
        }
    }
    pub fn flush(&mut self) -> Option<io::ErrorKind> {
        if self.errors >= 3 || self.buf.is_empty() {
            return None;
        }
        let text = std::mem::take(&mut self.buf);
        let result = (|| -> io::Result<()> {
            append_private(&self.path, text.as_bytes())?;
            let size = match self.size {
                Some(n) => n + text.len() as u64,
                None => fs::metadata(&self.path)?.len(),
            };
            self.size = Some(size);
            if size > self.cfg.max as u64 {
                let mut file = fs::File::open(&self.path)?;
                file.seek(SeekFrom::Start(size.saturating_sub(self.cfg.keep as u64)))?;
                let mut bytes = Vec::new();
                file.read_to_end(&mut bytes)?;
                drop(file);
                let kept = complete(&bytes);
                atomic_write_private(&self.path, kept.as_bytes())?;
                self.size = Some(kept.len() as u64);
            }
            Ok(())
        })();
        result.err().map(|e| {
            self.errors += 1;
            e.kind()
        })
    }
}
