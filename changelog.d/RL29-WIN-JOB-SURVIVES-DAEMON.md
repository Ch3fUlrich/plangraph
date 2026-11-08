Windows runs retain their kill-on-close Job through an independent private
pg-spawnd handle holder, allowing existing named-Job recovery after daemon loss.
The holder owns only non-inheritable QUERY/TERMINATE rights and exits on an empty
Job or, only if still unadopted, terminates it at its fixed launch deadline. Startup validates the daemon
setting PG_SPAWND_WINDOWS_HOLDER_MAX_S (default 28800, range 600–172800 seconds).
Windows records include holder_deadline_ms; adoption never renews it. Verified
adoption signals a retained named Event: expiry then closes the holder without
terminating the Job owned by the adopting daemon. Card max_s
remains ignored. Job access is restricted to the daemon user and SYSTEM; helper
launch/ownership failures prevent payload resume. Linux execution is unchanged.
Native Windows execution remains a separately gated CI step.
