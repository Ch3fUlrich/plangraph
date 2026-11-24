`plangraph ci-watch` files CI-FIX items only for main and open non-draft pull requests that are not `CI probe:`
or landing-train (`land/*`) pull requests. After each scan a sweep cancels the open CI-FIX items of a pull
request that closed, became a draft, probe or train, or moved to a new head (the failing run's head is now
recorded as `sha` in the task state), with the reason in the keeper. The first run with `--keeper` cancels
the backlog; the sweep line is `cancel: <key> (<reason>)`.
