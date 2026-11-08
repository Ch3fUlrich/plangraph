Fixed reachable-blob secret scanning to retain raw bytes losslessly across chunk edges.
Reuse the shared ASCII/UTF-16 policy at doubling checkpoints and EOF, preserving patterns
and refusals without a size cap. Retained memory and cumulative scanned input are O(blob
size), including for clean letter-only blobs; memory is not a bounded overlap window.
Added multi-MiB work accounting and exact-edge encoding regression coverage.
For N blob bytes, carry capacity can approach 2N; five decoded lanes total at most
3N bytes (N ASCII + four N/2 UTF-16), processed sequentially with at most 1.5N
decoded text live, plus redaction's linear temporary strings. Tests cover CHUNK,
2*CHUNK and 4*CHUNK edges and EOF completion in all five lanes.
