Windows holder adoption recovers after an uncommitted requester timeout. Stale
or dead requester PIDs no longer abort the keeper, and live-owner handover is
admitted without starting an orphan deadline. Event operations check Win32 errors.
Native Windows coverage bounds payload readiness, retries re-adoption, verifies
Job membership after owner losses, and exercises timeout followed by adoption.
Completed exchanges release the request slot; its PID CAS is the cancellation latch.
Committed requests await acknowledgement or holder exit. Holder-loop Win32 errors retry without closing
the live Job. Native coverage also exercises dead PIDs and live handover expiry.
The shared native polling timeout is 10 seconds to cover orphan expiry and slower Windows CI scheduling.
