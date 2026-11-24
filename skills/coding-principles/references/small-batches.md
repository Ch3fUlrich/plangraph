# Principle 10 — Develop on small batches; run the full data once (rigid)

Read before any job over more than a small batch of data and before any investigation of a corpus.

A defect found on the full data costs a full rerun; one found on ten items costs
seconds. Investigation and development iterate on the **smallest input that can
show the effect**, and the full dataset is processed **once**, when the method has
stopped changing. Measured 2026-10-08: three investigators each launched a
corpus-wide pass to answer a question a 10-recording batch would have answered.
That held a shared 32-core machine at 99 % for hours, and each answer would have
arrived after the method had already changed again.

1. **Climb the data ladder; never skip a rung.**
   1. *Small artificial data with a known output*: a fixture whose expected result
      you can state before running. It proves the logic.
   2. *A small slice of real data*: the known examples plus a handful of items chosen
      to **cover the variation** (sizes, cohorts, edge cases). It proves the code
      meets reality's formats and edge cases. Investigate the differences here,
      refine, and repeat.
   3. *The full data*: only when rungs 1 and 2 are green and the method is final.
      Prefer making it the production run's own dry run over a separate pass.

   A red result at any rung sends you back to rung 1, with a fixture that
   reproduces the failure.
2. **An iteration takes minutes, not hours.** If it does not, the batch is too big
   or the work is not cached. Persist per-item intermediates so the next iteration
   reuses them.
3. **Read what is already stored; never recompute it at scale.** A count, coverage
   figure or distribution that the store, the database or an earlier run already
   holds is a cheap read even over everything. Recomputing it is a full pass in
   disguise.
4. **Label every number with its sample** (how many items it came from), and say
   which figures are extrapolated.
5. **Share the machine.** A job that genuinely has to run at scale runs one at a
   time, with capped workers and BLAS threads, at below-normal priority, so the
   person using the machine and the other jobs are not starved.

| Red flag | Reality |
|---|---|
| "I'll run it on everything to see what happens" | Hours per question, and the answer lands after the method changed. Run the batch. |
| "The sample might miss something" | Choose it to cover the variation. When a miss turns up, add it to the batch. The full run still happens once, at the end. |
| "It is read-only, so a full pass is harmless" | It still costs hours of a shared machine and stalls every iteration behind it. |
| "Let me compute the corpus-wide distribution" | Is it already stored? Read it. |
