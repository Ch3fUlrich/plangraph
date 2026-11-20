Rust fleet series core now retains at most 1,440 samples and 4 MiB per host,
reading only a bounded tail even for legacy oversized files (pr866b-haiku #9).
Adds minute summaries, adjacent OOM/session-loss annotation and terminal run OOM
attribution from the run's stored host. Missing coverage remains unknown.
First self-contained slice under 600 changed lines: fleet collection, sampler CLI,
and systemd units remain open. No live host or timer changes.
