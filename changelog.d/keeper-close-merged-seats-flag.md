The keeper setting `require_seats` (default off) decides whether a merged close needs witnessed
review seats. With it off, `close_merged` and the automatic merge close finish on the merge evidence
and store the flag `seats_missing` with the cited run ids instead of refusing `NO-WITNESS`; the
merge-close job no longer forces seats on when its environment switch is unset.
