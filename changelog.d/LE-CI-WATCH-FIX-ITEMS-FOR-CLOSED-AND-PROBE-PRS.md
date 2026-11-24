`plangraph ci-watch` files CI-FIX items only for main and open non-draft pull requests that are not `CI probe:`
or landing-train (`land/*`) pull requests. After each scan a sweep cancels the open CI-FIX items of a pull
request that closed, became a draft, probe or train, or moved to a new head (the failing run's head is now
recorded as `sha` in the task state), with the reason in the keeper. The first run with `--keeper` cancels
the backlog; the sweep line is `cancel: <key> (<reason>)`. The sweep runs before the scan, reads the head of older rows from the stored run, and without `--keeper` keeps the row. A failing landing train still reaches the writer recorded for it (never a fix item), a pending cancel is kept for rows without a stored head, and every key an item can live under is cancelled before its row goes.
