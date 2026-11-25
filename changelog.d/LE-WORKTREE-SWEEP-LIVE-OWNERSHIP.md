pg-worktree-sweep and the reaper now report `live-run` only for a tree owned by an unfinished (or survivor-holding) run record whose checkout or attempt is at or inside the tree, and `pinned` for operator pins and reserved names; both still protect, and a run recorded only in an ancestor directory no longer counts as owning the trees below it (the reaper still refuses to move such a tree). `pg-reap` facts without `pinned` still parse as false.

Ownership evidence is the set of distinct `(record.json run_id, inventoried worktree)` pairs: for each unfinished run (status outside the releasing set of `sweep_runs::finished`, or any survivor) every recorded checkout, `cwd`, run worktree and attempt path is matched to the registered Git worktrees (main excluded) that hold it, the rule of `Guards::live_run_owns` (a recorded path at or inside the worktree). A path recorded only above a worktree, or inside no registered worktree, owns nothing. Path values are read as `sweep_runs::paths` reads them (a string or an object with a `path`), paths keep their bytes (NUL-delimited), a run record without a `run_id` or an invalid path stops the command, and so does a failed Git inventory, so a failure never prints as zero owners. The earlier claim that the workstation's 153 of 245 `live-run` rows were pins is withdrawn.

```sh
set -euo pipefail
# STATE: run-state directory; REPO: the repository whose worktrees are swept.
tmp=$(mktemp -d)
git -C "$REPO" worktree list --porcelain -z | grep -az '^worktree ' | sed -z 's/^worktree //' |
  tail -z -n +2 | xargs -0 -r realpath -m -z -- > "$tmp/inventory"
for r in "$STATE"/runs/*/record.json; do
  jq -j 'def p: if type=="object" then .path else . end;
    select(((.status|IN("completed","failed","cancelled","stalled","stalled_no_leg","timeout","refused","dry_run","timed_out","killed","succeeded","skipped","terminated_after_answer")) and ((.survivors//[])|length==0))|not)
    | (.run_id // error("unfinished record without run_id")) as $id
    | [.checkout,.cwd,.run_worktree.path,((.worktree_attempts//[])[].path)] | .[] | select(. != null) | p
    | (if type=="string" and startswith("/") then . else error("invalid run checkout") end)
    | ($id, "\u0000", ., "\u0000")' "$r"
done > "$tmp/recorded.raw"
while IFS= read -r -d '' id && IFS= read -r -d '' p; do
  printf '%s\0' "$id"; realpath -m -z -- "$p"
done < "$tmp/recorded.raw" > "$tmp/recorded"
jq -rn --rawfile w "$tmp/inventory" --rawfile r "$tmp/recorded" '
  ($w|split("\u0000")|map(select(. != ""))) as $w | ($r|split("\u0000")|map(select(. != ""))) as $r
  | [range(0;$r|length;2) | {id:$r[.],p:$r[.+1]}]
  | map(. as $x | $w[] | select(. as $d | $x.p==$d or ($x.p|startswith($d+"/"))) | [$x.id,.])
  | unique | .[] | @tsv'
```

The distinct worktree count is the number of distinct second fields. On the development host (2979 readable records, 1 unfinished `running` run whose recorded `cwd` is an unregistered review checkout) the join prints no line: 0 pairs, 0 owned worktrees. The workstation counts previously quoted for this change (609 records, one unfinished `limited` run, `dry_run` finished) were taken from the recorded paths alone and are to be replaced by this command's output on the workstation before the result is reported.

Deploy: no deploy needed beyond the normal rebuild of `pg-worktree-sweep` and the reaper binaries.
