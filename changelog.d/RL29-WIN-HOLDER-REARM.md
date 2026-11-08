Windows holder adoption recovers after an uncommitted requester timeout. Stale
or dead requester PIDs no longer abort the keeper, and live-owner handover is
admitted without starting an orphan deadline. Event operations check Win32 errors.
Native Windows coverage bounds payload readiness, retries re-adoption, verifies
Job membership after owner losses, and exercises timeout followed by adoption.
