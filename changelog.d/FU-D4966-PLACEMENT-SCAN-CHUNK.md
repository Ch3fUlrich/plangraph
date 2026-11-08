Fixed reachable-blob secret scanning to retain raw bytes losslessly across chunk edges.
Reuse the shared ASCII/UTF-16 policy at doubling checkpoints and EOF, preserving patterns
and refusals without a size cap. Retained memory and cumulative scanned input are O(blob
size), including for clean letter-only blobs; memory is not a bounded overlap window.
Added multi-MiB work accounting and exact-edge encoding regression coverage.
