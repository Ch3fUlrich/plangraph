Treat ESRCH while reading a disappearing /proc entry as a vanished process during
leader reconciliation, just like ENOENT. Keep live-group, malformed-stat and
permission-error checks conservative; cover the reaping race deterministically
without retries or delays.
