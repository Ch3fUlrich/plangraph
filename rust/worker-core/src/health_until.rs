//! Pure reset precedence. RL18 supplies usable fresh observations, never poll times.
use crate::{
    health::{self, Health, Row, State, UntilSrc},
    hosts::Role,
};

#[cfg(test)]
#[path = "health_until_tests.rs"]
mod tests;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderState {
    pub until: Option<i64>,
    pub observed_s: i64,
}

/// No I/O or persistence: `None` means the reader has no usable overlay.
pub fn fold_with_provider(
    leg_key: &str,
    route_key: &str,
    role: Role,
    rows: &[Row],
    now_s: i64,
    provider: Option<&ProviderState>,
) -> Health {
    health::replay(leg_key, route_key, role, rows, now_s, provider)
}

pub(crate) struct ReplayReset<'a> {
    provider: Option<&'a ProviderState>,
    settled: bool,
    response: Option<(i64, i64)>,
}

impl<'a> ReplayReset<'a> {
    pub(crate) fn new(provider: Option<&'a ProviderState>) -> Self {
        Self {
            provider,
            settled: false,
            response: None,
        }
    }

    fn deadline(p: &ProviderState) -> i64 {
        p.until
            .unwrap_or_else(|| p.observed_s.saturating_add(86400))
    }

    fn overlay(&self, h: &mut Health, ts: i64) {
        if let Some(p) = self
            .provider
            .filter(|p| !self.settled && p.observed_s <= ts)
        {
            if self
                .response
                .is_some_and(|(observed, _)| observed >= p.observed_s)
            {
                return;
            }
            h.until_s = Some(Self::deadline(p));
            h.until_src = Some(UntilSrc::OmniRoute);
            h.reset_assumed = p.until.is_none();
            h.reset_clamped = false;
            h.state = if ts >= Self::deadline(p) {
                State::HalfOpen
            } else {
                State::Open
            };
        }
    }

    pub(crate) fn before(&mut self, h: &mut Health, ts: i64) {
        self.overlay(h, ts);
        if self
            .provider
            .is_some_and(|p| ts >= p.observed_s && ts >= Self::deadline(p))
        {
            self.settled = true;
        }
    }

    pub(crate) fn success(&mut self) {
        self.response = None;
    }

    pub(crate) fn open(&mut self, h: &mut Health, row: &Row, reset: Option<&Row>, probe: bool) {
        let previous = self.response;
        if row.outcome.carries_reset() {
            self.response = None;
            if let Some(r) = reset {
                if let Some(raw) = r
                    .reset_at
                    .filter(|raw| exact(*raw) && exact(r.ts) && exact(r.ts.saturating_add(86400)))
                {
                    self.response = Some((r.ts, raw));
                }
            }
            if self.response.is_none() && probe {
                self.response = previous.filter(|(ts, raw)| *raw > ts.saturating_add(86400));
            }
        }
        if let Some((observed, raw)) = self.response.filter(|(ts, raw)| {
            row.outcome.carries_reset() || (probe && *raw > ts.saturating_add(86400))
        }) {
            let anchor = if probe { row.ts } else { observed };
            let (until, clamped) = crate::reset::clamp_until(raw as f64, anchor as f64);
            if exact(anchor) && until.abs() <= 9_007_199_254_740_991.0 {
                h.until_s = Some(until as i64);
                h.until_src = Some(UntilSrc::Row);
                h.reset_assumed = false;
                h.reset_clamped = clamped;
            }
        }
        self.overlay(h, row.ts);
    }

    pub(crate) fn finish(&self, h: &mut Health, now_s: i64) {
        self.overlay(h, now_s);
    }
}

fn exact(ts: i64) -> bool {
    ts.unsigned_abs() <= 9_007_199_254_740_991
}
