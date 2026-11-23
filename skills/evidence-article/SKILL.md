---
name: evidence-article
description: Use when writing or updating an article in the evidence folder. One folder per measured change, written like a blog post a newcomer can follow, with its data, queries and sources beside the text and the front matter the index is generated from.
---

# evidence-article

An evidence article records one improvement so that a later project can find it, trust it
and reuse it: what the problem was, what was decided and why, what was measured, what did
not work. It reads like a good engineering blog post and carries its data with it.

The evidence comes from `skills/evidence-acquisition/SKILL.md`. Do that first; an article
without its data files is not an article.

## Where it lives

The folder `evidence/` of the private data repository, never a public repository: articles
name decisions, hosts and telemetry. One folder per measured change, or per rollout when
several changes shipped together and their effects cannot be told apart.

```
evidence/<yyyy-mm-dd>-<slug>/
  article.md       front matter and text
  claims.jsonl     every number in the text, with its query and data file
  sources.jsonl    decisions, items, commits, runs, reports and pages the text relies on
  manifest.json    file list with sha256, windows, tool versions
  data/raw/  data/derived/  queries/  figures/
```

## Front matter

`id`, `title`, `date`, `owner`, `status` (`draft`, `accepted`, `superseded`, `withdrawn`),
`evidence_class` (`prospective` or `retrospective`),
`decision_ids`, `item_keys`, `commits`, `components`, `stage`, `level`, `tags`,
`supersedes`, `superseded_by`, `last_reviewed`, and one `results` entry per measured
metric: `metric`, `unit`, `direction`, `design` (`paired`, `switchback`, `before_after`,
`none`), `baseline` and `after` (each with value, window and sample size), `effect`
(absolute and relative), `certainty`, `outcome`, `gain_class` (`local`, `system`,
`later_improvement`), `claim_id`. A value that does not exist is `null` with a reason.

## Outline

1. **The result in one paragraph.** The number first: baseline, after, sample size, what
   was changed, and the main caveat. A result that showed nothing is stated just as
   plainly.
2. **The situation.** What the component does and why it mattered, for a reader who was
   not there.
3. **The problem and how it was measured.** The metric and its definition, the baseline
   window, where the time or cost went. Keep what was observed apart from what was
   assumed to be the cause.
4. **The options.** What was considered, with the reason each was taken or rejected.
5. **The decision.** The decision id, who decided, and the result that was expected.
6. **What was done.** Items, commits, configuration, the date it went live.
7. **The measured effect.** Design, windows, sample sizes, effect and certainty, the
   guardrail metrics, what else changed in the window.
8. **What did not work.** Failed attempts, regressions, reversals, and where luck helped.
   This section is required; write "nothing failed" only when that is true.
9. **What it means for later work.** When the result applies, when it does not, and the
   check that would show it no longer holds.
10. **Open questions.**
11. **Data appendix.** A table of the files, the queries and how to run them again.

## Writing rules

- Write for a reader who arrives cold: full sentences, terms defined at first use, no
  session shorthand, no arrows or symbols in place of words.
- One claim per paragraph, each number followed by its claim id. No number without a
  line in `claims.jsonl`.
- State uncertainty where it applies: sample size, certainty level, and what would change
  the conclusion. Do not add percentage gains of overlapping changes.
- Say no more than the data carries. A before-and-after difference is an observed
  difference, not a cause; an order of changes is not a ranking of causes; a peak is not
  a rate. Give each metric its own window, and a count its counting rule.
- A retrospective article lists what was never measured as plainly as what was.
- A figure only when it explains a mechanism or a comparison; its table is in
  `data/derived/`.
- The decision part fits on two pages; detail goes to the appendix.

## Index and lifecycle

- `evidence/index.jsonl` is generated from the front matter, one line per article and
  result; `evidence/README.md` lists the articles for readers. Neither is edited by hand.
- An accepted article's result is never edited. A newer measurement is a new article; the
  old one gets `superseded_by` and stays.
- The article that sums up many changes (the state of the system) links to the single
  articles and recomputes its totals from the daily series.
- An article is knowledge. It cites the rule or decision; it is never the source of one.

## Before the status becomes accepted

- Reader test: an agent of another model family that sees only the folder answers the
  questions a later project would ask (what changed, by how much, how sure, does it apply
  to me). Where it fails, the text is fixed, not the question.
- Every number was produced again from its query on the day of acceptance.
- "What did not work" and "What it means for later work" are filled.
- No secret, token or credential path appears in text or data.
