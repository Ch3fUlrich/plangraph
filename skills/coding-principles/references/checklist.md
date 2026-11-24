# Checklist

Run through this before reporting an implementation, refactor or bugfix done.

- [ ] Read the source of each rule that applies; navigated by symbol, not by whole files (Principle 6).
- [ ] Wrote a failing test before implementation (Principle 2).
- [ ] Placed each new fact/logic in exactly one home (Principle 1).
- [ ] Each new unit has a single responsibility (Principle 3).
- [ ] Documented the *why* in the same change (Principle 4).
- [ ] Committed small; logged the change / ADR for backtracking (Principle 5).
- [ ] Kept repository clean; created scratch files only in connected `scratch/` folders (Principle 7).
- [ ] Named what derives from / points at the thing being changed, and moved it in the same change (Principle 8).
- [ ] Selected by the condition that makes records wrong, not by the symptom already seen (Principle 8).
- [ ] Verified through the branch production actually takes, not a convenient fallback (Principle 9).
- [ ] Iterated on synthetic data and a small, varied real batch; ran the full data at most once, at the end (Principle 10).
- [ ] Every new path writes its run record, a wait says since when and why, and stored state is not rebuilt per read (Principle 11).

