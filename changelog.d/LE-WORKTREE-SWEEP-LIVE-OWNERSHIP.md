pg-worktree-sweep and the reaper now report `live-run` only for a tree owned by an unfinished (or survivor-holding) run record whose checkout or attempt is at or inside the tree, and `pinned` for operator pins and reserved names; both still protect, and a run recorded only in an ancestor directory no longer counts as owning the trees below it (the reaper still refuses to move such a tree). `pg-reap` facts without `pinned` still parse as false.

Ownership evidence is the set of distinct `(record.json run_id, inventoried worktree)` pairs: for each unfinished run (status outside the releasing set of `sweep_runs::finished`, or any survivor) every recorded checkout, `cwd`, run worktree and attempt path is matched to the registered Git worktrees (main excluded) that hold it, the rule of `Guards::live_run_owns` (a recorded path at or inside the worktree). A path recorded only above a worktree, or inside no registered worktree, owns nothing. A run record without a `run_id` is skipped, never named by its directory. The earlier claim that the workstation's 153 of 245 `live-run` rows were pins is withdrawn.

```sh
# STATE: run-state directory; REPO: the repository whose worktrees are swept; TMP: scratch directory.
git -C "$REPO" worktree list --porcelain | sed -n 's/^worktree //p' | tail -n +2 | while read -r w; do realpath -m "$w"; done | sort -u > "$TMP/inventory.txt"
for r in "$STATE"/runs/*/record.json; do
  jq -r 'select(.run_id and (((.status|IN("completed","failed","cancelled","stalled","stalled_no_leg","timeout","refused","dry_run","timed_out","killed","succeeded","skipped","terminated_after_answer")) and ((.survivors//[])|length==0))|not))|.run_id as $rid|[.checkout,.cwd,.run_worktree.path?,(.worktree_attempts[]?.path)]|.[]|select(type=="string")|[$rid,.]|@tsv' "$r"
done | while IFS=$'\t' read -r id p; do printf '%s\t%s\n' "$id" "$(realpath -m "$p")"; done | sort -u > "$TMP/recorded.txt"
awk -F'\t' 'NR==FNR{inv[$0]=1;next}{for(w in inv)if($2==w||index($2,w "/")==1)print $1"\t"w}' "$TMP/inventory.txt" "$TMP/recorded.txt" | sort -u
```

The distinct worktree count is the number of distinct second fields. On the development host (2979 readable records, 1 unfinished `running` run whose recorded `cwd` is an unregistered review checkout) the join prints no line: 0 pairs, 0 owned worktrees. The workstation counts previously quoted for this change (609 records, one unfinished `limited` run, `dry_run` finished) were taken from the recorded paths alone and are to be replaced by this command's output on the workstation before the result is reported.

Deploy: no deploy needed beyond the normal rebuild of `pg-worktree-sweep` and the reaper binaries.
