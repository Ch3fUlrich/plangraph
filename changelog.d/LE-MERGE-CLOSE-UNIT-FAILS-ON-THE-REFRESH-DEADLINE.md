The scheduled merge-close pass ends a keeper index refresh that is still running as a counted `refresh_pending` line with exit 0 (third in a row fails), a continue timer reads the finished scan within its freshness window, and the keeper bounds a scan at 180 s, refusing closes with `merge_index_failed` instead of publishing an over-budget scan.

Keeper now reports one payload-free `merge_index_refresh` record per completed
background generation. Stage durations and attempts, nested forge call counts
and landing cache hits let operators attribute slow refreshes from the journal.

Background refreshes now overlap up to four pinned deploy reads with the
landing and lane lookups. The refresh thread records the results and the journal
reports summed stage work with residual wall time.
