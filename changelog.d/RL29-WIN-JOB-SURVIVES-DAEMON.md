Windows runs retain their kill-on-close Job through an independent private
pg-spawnd holder, allowing named-Job recovery after daemon loss. The holder
waits for the owning daemon process to exit before starting the bounded orphan
window. A live daemon's run is never expired. Empty Jobs release the holder;
unadopted Jobs are terminated when the orphan window ends.

Windows alone validates PG_SPAWND_WINDOWS_HOLDER_MAX_S (default 28800, range
600–172800 seconds). Verification is read-only; restarted-daemon adoption
explicitly signals a restricted, collision-rejecting Event. Adoption is refused
within five seconds of expiry, and the holder rechecks adoption before killing.
Normal teardown explicitly terminates the Job. Embedders must register a holder
binary implementing the __job-holder dispatch; missing helpers and refused
breakaway fail closed before payload resume. Ticket handles are trusted only
over the private same-user helper pipe. Linux execution is unchanged.

Native Windows tests kill an owner subprocess and adopt from a separate process.
