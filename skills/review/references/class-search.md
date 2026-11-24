# Class-Search check

Read when you validate the fix of a failing test or review finding.

For each fix of a failing test or review finding, verify the whole-class regression and use
`pg-policy --class-search FIX_SHA` to check its `Class-Search: <pattern> -> <n> hits, <m> changed` line.
On a checkout of the branch under review, a descendant commit can record
`Class-Search: <fix-sha8> <pattern> -> <n> hits, <m> changed` in its body.
Use `pg-policy --class-search --train-delta FIX_SHA` only for a train delta review
that covers the resolution. It exempts single-parent `Train N: resolve` commits.
