Add `plangraph land-timeline experiment`: batches and items merged per hour, full test runs per
landed batch and red trains per day over the last N days against the N days before, with the keep
rule (batches per hour rose). `record --items N` stores the item count on the merge row.
A test run is a workflow run and attempt, and a combined train counts the runs of its predecessors.
