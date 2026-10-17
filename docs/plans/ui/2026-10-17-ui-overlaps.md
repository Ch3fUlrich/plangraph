# UI overlaps: scope narrowing (2026-10-17)

plangraph-L1-plan2, after the restructure D-1747 (option A: no cancels, ordering edges only; scope narrowing is plan2's call).
Batch: `2026-10-17-ui-overlaps.yaml` (4 edits: title, done criteria, note). There are no dependency changes; the restructure already added
the edges.

## Who owns what

| Concern | Owner | Not |
|---|---|---|
| Operator role and the verbs operator_answer, task_done and skip | QA-VERBS | PG-ANSWER-UI |
| General GUI write auth (OIDC code + PKCE, CSRF) for every write | API-OIDC | PG-ANSWER-UI |
| Interim answer path: the operator principal from the edge proxy hop (Authelia user header, mTLS hop of QA-GATE), source ui, md-sync mirror | PG-ANSWER-UI | QA-VERBS, API-OIDC |
| Answer card component and Needs-you rows (VIEWS.md section 6), copy line, POST behind a flag | QA-UI | PG-QUESTION-CARDS |
| Guided answer flow: one card at a time, Skip and Next, progress bar, why-it-matters context, ordered by unblocks N | PG-QUESTION-CARDS | QA-UI |
| Decision edit (a fourth operator verb) and the changed-since notice | PG-DEC-EDIT-NOTICE | PG-QUESTION-CARDS |

## Why the interim proxy path stays

- API-OIDC waits behind PG-M2B, the whole hardening milestone. If answers waited for it, the operator could not answer in the UI for
  a long time.
- So PG-ANSWER-UI keeps the proxy-hop path, but only for operator verbs (answers, task done, skip, and the decision edit). When
  API-OIDC lands, those writes move onto it, and UI-WRITES uses only API-OIDC.
- Decision edits ride the same interim path. The operator asked to edit decisions in the UI (D-1126), and a decision edit is the same
  class of write as an answer: operator only, a text record, and a hash-chained event.
