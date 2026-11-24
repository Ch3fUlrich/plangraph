Each feeder writer run now gets its own status file (`<review_dir>/status-<slug>-<key digest>-e<claim epoch>.txt`, reserved exclusively per launch with `create_new`, so no two launches share one),
carried from the card as `writer_status_path` and quoted by the brief as STATUS_FILE, so
concurrent batch runs no longer mix DONE lines in one `status.txt`. The shipped feeder policy
sets `batch_max_items = 3` and `pg-feeder.service` passes `--policy deploy/feeder/policy.toml`;
the deploy GO names this switch.
