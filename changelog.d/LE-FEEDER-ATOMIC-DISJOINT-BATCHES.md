The feeder tick can claim a priority-ordered batch of related ready items for one
writer run (policy `batch_max_items`, default 1). Each claim is checked against the
claimed items of every host, queued runs included; a claim whose files overlap another
live claim is released by its own epoch and skipped, so simultaneous hosts never keep
overlapping files. A failed launch releases only its own batch.
