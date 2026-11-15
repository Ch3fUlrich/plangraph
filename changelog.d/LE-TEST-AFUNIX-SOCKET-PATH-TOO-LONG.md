# LE-TEST-AFUNIX-SOCKET-PATH-TOO-LONG

Dell sandbox isolation probes now bind their host Docker canary socket in the
shared short socket directory under `/tmp`, independent of pytest's temp root
and `TMPDIR`. A test fixture rejects filesystem AF_UNIX binds under pytest temp
roots or with paths of 100 bytes or more; abstract sockets remain supported.
