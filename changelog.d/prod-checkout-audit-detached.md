- **LE-PROD-CHECKOUT-DETACHED-NO-ALARM:** Detach HEAD audit runtimes into a separate session with discarded standard streams, retaining the one-second deadline without making Git wait on stuck audit I/O.
Preserve originating Git process chains before detaching, and poll for eventual audit writes in HEAD-move, linked-worktree and rotation regressions.
