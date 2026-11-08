# Plan base: pointer to the private originals

The orchestration-setup design ([2026-10-16-orchestration-setup.md](2026-10-16-orchestration-setup.md)) builds on two
base documents. Both hold site-specific text, so they stay in the private repository and are not copied here
(D-4910: plangraph is public; no operator-round text, scrubbed or not, in this repository).

| Base document | Private repository | Directory | File | Commit | sha256 of the file |
|---|---|---|---|---|---|
| Rethink of the orchestration setup, design (D-1285, approved) | `Ch3fUlrich/routing` | `docs/reports/` | `2026-10-15-rethink-setup-design.md` | `6ff782a0c3b8f39d7d302c3e6f2cd172a27e9107` | `1958911c9a9c4b3a301096e0e296a887c75e0bcaeaa5d94cee27a6d550a2309e` |
| Operator rounds, verbatim log (D-1292) | `Ch3fUlrich/routing` | `docs/plan/rethink/` | `operator-rounds.md` | `6ff782a0c3b8f39d7d302c3e6f2cd172a27e9107` | `ff41ee5ce25bd193b7c99c436924584ba450299eac3fee5f952e09875aad8eb7` |

Check (with read access to the private repository):

```sh
C=6ff782a0c3b8f39d7d302c3e6f2cd172a27e9107
git -C routing show "$C:docs/reports/"2026-10-15-rethink-setup-design.md | sha256sum
git -C routing show "$C:docs/plan/rethink/"operator-rounds.md | sha256sum
```

Where the design and the base differ, the base wins (design section 11). Corrections go in new files, never in the base.
