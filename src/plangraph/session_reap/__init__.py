"""plangraph.session_reap - background session reaping for Claude Code (SESSION-AUTO-CLOSE, D-2442)."""

from .scan import SessionRecord, ScanResult, scan, scan_jobs
from .policy import decide, ReapConfig, DEFAULT_ALLOWLIST, DEFAULT_DONE_GRACE_MIN, DEFAULT_IDLE_MIN
from .act import stop_session, default_runner
from .log import log_stop, DEFAULT_LOG_PATH
from .mark import mark_session

__all__ = [
    "SessionRecord",
    "ScanResult",
    "scan",
    "scan_jobs",
    "decide",
    "ReapConfig",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_DONE_GRACE_MIN",
    "DEFAULT_IDLE_MIN",
    "stop_session",
    "default_runner",
    "log_stop",
    "DEFAULT_LOG_PATH",
    "mark_session",
]
