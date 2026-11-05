//! Output-only capture. No process startup and no run-record persistence.
use super::{
    activity::is_activity,
    livelog::{LiveLog, LiveLogCfg},
    scrub::Scrubber,
    tail::{Keeper, RECENT, STDOUT_HEAD, STDOUT_TAIL},
};
use serde_json::{json, Value};
use std::{
    io::{self, Read},
    path::PathBuf,
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};
#[derive(Clone, Copy)]
pub enum Stream {
    Stdout,
    Stderr,
}
pub struct CaptureCfg {
    pub live_log: Option<PathBuf>,
    pub live_cfg: LiveLogCfg,
    pub scrubber: Scrubber,
}
pub struct Snapshot {
    pub stdout_recent: String,
    pub stderr_recent: String,
    pub stdout_final: String,
    pub stderr_final: String,
    pub last_activity_ts: Option<f64>,
    pub seq: u64,
}
struct Input {
    decoder: Vec<u8>,
    pending: String,
    done: bool,
    keeper: Keeper,
}
struct State {
    inputs: [Input; 2],
    scrubber: Scrubber,
    log: Option<LiveLog>,
    events: Vec<Value>,
    ts: Option<f64>,
    seq: u64,
}
pub struct Capture {
    state: Mutex<State>,
}
impl State {
    fn event(&mut self, error: Option<io::ErrorKind>) {
        if let Some(error) = error {
            self.events
                .push(json!({"event":"live_log_error", "error":format!("{error:?}")}));
        }
    }
    fn consume(&mut self, index: usize, bytes: &[u8], finish: bool) {
        let input = &mut self.inputs[index];
        if input.done {
            return;
        }
        input.decoder.extend_from_slice(bytes);
        let mut offset = 0;
        while offset < input.decoder.len() {
            match std::str::from_utf8(&input.decoder[offset..]) {
                Ok(s) => {
                    input.pending.push_str(s);
                    offset = input.decoder.len();
                }
                Err(e) => {
                    let end = offset + e.valid_up_to();
                    input
                        .pending
                        .push_str(std::str::from_utf8(&input.decoder[offset..end]).unwrap());
                    offset = end;
                    match e.error_len() {
                        Some(n) => {
                            input.pending.extend(std::iter::repeat_n('\u{fffd}', n));
                            offset += n;
                        }
                        None if finish => {
                            input.pending.push('\u{fffd}');
                            offset = input.decoder.len();
                        }
                        None => break,
                    }
                }
            }
        }
        input.decoder.drain(..offset);
        input.done = finish;
        let end = if finish || input.pending.len() > 65536 {
            input.pending.len()
        } else {
            input.pending.rfind(['\n', '\r']).map_or(0, |n| n + 1)
        };
        let block: String = input.pending.drain(..end).collect();
        if block.is_empty() {
            return;
        }
        let active = block.split_inclusive(['\n', '\r']).any(is_activity);
        let text = self.scrubber.scrub(&block);
        let final_text = |k: &Keeper| {
            if index == 0 {
                k.render()
            } else {
                k.recent(RECENT)
            }
        };
        let before = (final_text(&input.keeper), input.keeper.recent(RECENT));
        input.keeper.push(&text);
        let mut changed = before != (final_text(&input.keeper), input.keeper.recent(RECENT));
        if active {
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64();
            changed |= self.ts != Some(now);
            self.ts = Some(now);
        }
        if changed {
            self.seq += 1;
        }
        let error = self.log.as_mut().and_then(|log| log.feed(&text));
        self.event(error);
    }
}
impl Capture {
    pub fn new(cfg: CaptureCfg) -> Self {
        let input = |keeper| Input {
            decoder: Vec::new(),
            pending: String::new(),
            done: false,
            keeper,
        };
        Self {
            state: Mutex::new(State {
                inputs: [
                    input(Keeper::new(STDOUT_HEAD, STDOUT_TAIL)),
                    input(Keeper::new(0, RECENT)),
                ],
                scrubber: cfg.scrubber,
                log: cfg.live_log.map(|p| LiveLog::new(p, cfg.live_cfg)),
                events: Vec::new(),
                ts: None,
                seq: 0,
            }),
        }
    }
    pub fn feed(&self, stream: Stream, bytes: &[u8]) {
        self.state
            .lock()
            .unwrap()
            .consume(stream as usize, bytes, false);
    }
    pub fn finish(&self, stream: Stream) {
        self.state
            .lock()
            .unwrap()
            .consume(stream as usize, &[], true);
        self.flush_live();
    }
    pub fn flush_live(&self) {
        let mut s = self.state.lock().unwrap();
        let error = s.log.as_mut().and_then(LiveLog::flush);
        s.event(error);
    }
    pub fn snapshot(&self) -> Snapshot {
        let s = self.state.lock().unwrap();
        Snapshot {
            stdout_recent: s.inputs[0].keeper.recent(RECENT),
            stderr_recent: s.inputs[1].keeper.recent(RECENT),
            stdout_final: s.inputs[0].keeper.render(),
            stderr_final: s.inputs[1].keeper.recent(RECENT),
            last_activity_ts: s.ts,
            seq: s.seq,
        }
    }
    pub fn take_events(&self) -> Vec<Value> {
        std::mem::take(&mut self.state.lock().unwrap().events)
    }
}
pub fn pump(mut reader: impl Read, stream: Stream, capture: &Capture) {
    let mut bytes = [0; 65536];
    loop {
        match reader.read(&mut bytes) {
            Ok(0) => break,
            Ok(n) => capture.feed(stream, &bytes[..n]),
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        }
    }
    capture.finish(stream);
}
