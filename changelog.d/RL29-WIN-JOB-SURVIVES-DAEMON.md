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

Round 4 confines teardown to Windows, opens legacy Jobs without requiring holder
events until adoption, and uses monotonic orphan deadlines. Adoption now requires
the holder's explicit acknowledgement before reporting success; admission and
expiry are serialized in the holder. A native test kills an owner without an
adopter and verifies termination after a one-second orphan window.

V7 remains unfixed: repeat-daemon-loss survival requires transferring the current
owner process handle and configured bound to the retained holder, with a repeatable
authenticated handshake and restart tests; that protocol does not fit this round's
300-line production budget safely. Adoption still protects only the first loss.
The base workflow has no rust-windows job, so no filter line was added.
After merging the rust-windows job, CI now includes run::job_holder_windows with the per-filter zero-test guard.
