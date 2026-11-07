//! Stable phase activity retry policy (also used while replaying old histories).
use pg_worker_core::config::Config;
use std::time::Duration;
use temporalio_common::RetryPolicy;
use temporalio_sdk::ActivityOptions;

pub(crate) fn activity_options(cfg: &Config) -> ActivityOptions {
    let retry = RetryPolicy::builder()
        .initial_interval(Duration::from_secs(2))
        .maximum_attempts(5)
        .build();
    ActivityOptions::with_start_to_close_timeout(cfg.start_to_close)
        .retry_policy(retry)
        .build()
}
