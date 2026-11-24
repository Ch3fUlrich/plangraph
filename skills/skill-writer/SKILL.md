---
name: skill-writer
description: Write, shorten, split or move a skill (SKILL.md, references, evals) without losing a rule. Use when a skill is created, rewritten, moved to a card or reference, or gains a rule.
---

You are the skill writer. You change skill files and their evals, nothing else.

## Rules

1. **Start from an observed failure.** Name the transcript, review finding or failed run
   the skill fixes, then write the evals, then the text. No failure, no new skill: the
   agent already knows the general case.
2. **One job per skill.** The name equals the directory: lowercase letters, digits and
   hyphens, at most 64 characters, verb or noun first, no `claude` or `anthropic`.
3. **The description is the trigger.** One line, at most 500 characters (hard cap 1024):
   what the skill does, then `Use when` plus the situations and keywords that call for it.
   Put the key case first. No workflow summary: an agent that reads one skips the body.
4. **Budget the body.** Target 120 lines and 1500 words (about 2000 tokens; tokens are
   characters divided by 4). Role skills stop at 150 lines (test), every `.md` at 500 lines
   (lint). Past the target, move detail into `references/`.
5. **Write only what the agent would get wrong.** Keep project facts, numbers, paths,
   commands, thresholds and non-obvious failure cases. Cut what any model already knows.
6. **Write imperatively.** One term per concept. Give a default, not a menu, plus one
   named escape hatch. State the why in a clause; avoid shouting (ALWAYS, NEVER) when a
   reason does the job. Fragile steps get the exact command; judgement steps get a goal.
7. **Disclose progressively.** `SKILL.md` holds what every run needs. Each file in
   `references/` is linked from `SKILL.md` with the condition that loads it ("Read
   `references/x.md` when ..."), sits one level deep and opens with a contents list when it
   passes 100 lines. A reference never links to another reference. Text an agent
   without file tools needs must reach it by the brief header or stay in `SKILL.md`.
8. **Put a rule that must always hold into code.** A hook, lint or test beats a sentence.
   Name the enforcing `path::test` in the rule's `source:`.
9. **Keep the public repo clean.** No host name, key, real user path, private project name,
   dated instruction or history. Decision ids and dates go in commits, not skill text.
10. **Cards use rule lines.** In `cards/*.md` write
    `R-<topic>-<nn>: <imperative>. (why: <=12 words; source: <test or measurement>)`;
    check with `python3 tools/skill-rules.py all`.

## Rewrite without losing a rule

Run this whenever existing text is shortened, split or moved. The diff is not the check.

1. Snapshot: note the old commit sha of every file you will touch and export the old
   skill directory to a scratch directory (`git archive <sha> skills/<name>`).
2. **Extract an inventory** from the old text, in a `mktemp` path, never in the repo:
   every rule sentence, number, threshold, path, command, flag, decision id and name, one
   line each. The tokens come from
   ```
   git show <sha>:<path> | grep -oE '`[^`]+`|[0-9][0-9A-Za-z.%]*|[RD]-[a-z0-9-]+' | sort -u
   ```
   and you add the sentences that hold a `must`, `never`, `only`, a number or a
   condition by hand.
3. **Assign a disposition** to every line: `keep` (in `SKILL.md`), `move` (a named
   `references/` file or card), `code` (a named `path::test`), or `delete` with a why of
   at most 12 words. Delete only text that is stale with evidence (retired model,
   superseded rule you cite, file that does not exist) or a duplicate you point to by
   file. When in doubt, keep.
4. Rewrite. Then verify mechanically: loop `grep -rqF -- "$token" skills/<name>` over the
   inventory and print `MISSING $token`; every `code` row names an existing test. Zero
   missing is the gate. Every token that stays missing needs a `delete` row with its why.
5. Check the sentences too: for each old sentence, search the new text for its main
   nouns and numbers; a sentence whose words are mostly absent is a `move` or `delete`
   row, never silent.
6. Put the disposition table (item, keep / moved-to / code / deleted-because) into the
   commit message body, and one bullet into a `changelog.d/<ITEM-KEY>.md` fragment.
   Do not add a markdown report file and no row to `docs/skills/rule-disposition.md`.
7. Give the old text, the new text and the inventory to a reviewer of another model
   family. Its one task: find an inventory line that the new text lost, weakened or
   changed (a number, a flipped condition). Fix and re-ask until it finds none.

## Evals

`skills/<name>/evals/evals.json` holds 2-3 items: `id` (unique across all skills),
`prompt`, `must` (1-3 phrases), `must_not` (1-3 phrases), optional `note`. Each `must`
phrase appears verbatim, case-insensitive, in the text of the skill's `.md` files; the
test fails otherwise. Write prompts as real users type them, one with a near-miss that
should not apply, and `must_not` phrases for the shortcut an agent takes without the skill.
A phrase check is not a behaviour check. Before you report, run each prompt on a fresh
agent without the skill (or with the old version) and with the new one. A phrase that
holds in both runs teaches nothing: replace it. Quote the result per eval.

## Anti-patterns

- A description that summarises the workflow, says "helps with X", or speaks in first person.
- A body that explains what the agent knows, lists five tools as equal options, or gives
  one worked example per language.
- A reference that links another reference, or a link with no condition to load it.
- A rule with no why or no check, or the same rule in two files (edit the one source).
- Shortening by feel: "tightened" with no inventory is how rules disappear.
- A skill for something a hook, lint or test can enforce.

## Versions

Git is the version. Do not put a version or date into the skill; the commit message names
what changed and why, and a `changelog.d/` fragment records it.

## Done check

Report each line with its verbatim result; skip none.

- `python3 tools/skill-rules.py lint skills/<name>` passes.
- `python3 -m pytest -q tests/test_skill_evals.py tests/test_skills_roles.py tests/test_skill_rules.py tests/test_skill_rule_disposition.py` passes.
- `wc -l` and `wc -w` of `SKILL.md` are inside rule 4; the description is one line.
- AGENTS.md names the skill with the moment to read it.
- The inventory loop printed no missing token; the other-family review found none.

## Report

Max 10 lines: files changed, old and new characters divided by 4 per skill directory, the
verbatim summary line of each check, inventory rows deleted or moved, and what you did
not finish.
