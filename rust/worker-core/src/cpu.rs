//! Thirty-second weak activity sampling; no spawn identity or platform policy.
use crate::activity::{Kind, Signal};
use std::io;

#[cfg(test)]
#[path = "cpu_tests.rs"]
mod tests;

pub struct CpuSample {
    pub total_ms: u64,
}

pub struct Sampler {
    threshold: u64,
    last_poll: Option<u64>,
    baseline: Option<u64>,
}

impl Sampler {
    pub fn new(cpu_active_ms: u64) -> Self {
        Self {
            threshold: cpu_active_ms.max(1),
            last_poll: None,
            baseline: None,
        }
    }

    pub fn poll(
        &mut self,
        now_ms: u64,
        read: &mut dyn FnMut() -> io::Result<CpuSample>,
    ) -> Option<Signal> {
        if let Some(last) = self.last_poll {
            if now_ms < last {
                self.baseline = None;
            } else if now_ms - last < 30_000 {
                return None;
            }
        }
        self.last_poll = Some(now_ms);
        let total = match read() {
            Ok(sample) => sample.total_ms,
            Err(_) => {
                self.baseline = None;
                return None;
            }
        };
        let previous = self.baseline.replace(total)?;
        let delta = total.checked_sub(previous)?;
        (delta >= self.threshold).then_some(Signal {
            kind: Kind::Weak,
            ts_ms: now_ms,
            tool: None,
        })
    }
}
