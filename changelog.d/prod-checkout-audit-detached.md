- **LE-PROD-CHECKOUT-DETACHED-NO-ALARM:** Detach HEAD audit runtimes into a separate session with discarded standard streams, retaining the one-second deadline without making Git wait on stuck audit I/O.
Preserve originating Git process chains before detaching, and poll for eventual audit writes in HEAD-move, linked-worktree and rotation regressions.
Resolve zero old OIDs synchronously from Git's local reflog before detaching.
Cap live audit launches at four with inherited nonblocking locks beside Git's hooks;
keep dedupe markers there too, avoiding audit state I/O in the hook. Ignore malformed
ancestor snapshots individually and tighten asynchronous audit regressions.
