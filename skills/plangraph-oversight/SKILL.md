---
name: plangraph-oversight
description: Use in the attended planning session once a plan or a change of direction is accepted. Check every beat that the change reaches code, production, the keeper and the running agents; turn every gap into a keeper item in the same turn; route decisions to routing and mechanical work to the orchestrator.
---

# plangraph-oversight

A planning session that changed direction stays on as the overseer. Its job is to make the
change real fast: nothing counts until it runs in production and the agents follow it.
Merged is not done. Defined is not done. A rule that only a session applies by hand is not
enforced.

## Roles

- Overseer (you): measure, find gaps, write keeper items, report to the operator. You do
  research and planning only.
- Routing: decision ids, priorities, edges, cancels, rule text, GO rows, closes. Ask it for
  every one of these; never set them yourself.
- Orchestrator: claims, batches, launches, deploys, sweeps. Send it the work; never do it.
- Resolve the live session name of a role right before every message; sessions hand off
  often and an old counter name fails.

## Every beat

Measure each line from its source, never from memory or an earlier message. Report the
number with its time.

1. **Landing pace.** The objective is real work merged per time: items and batches merged
   on main per hour, today and over seven days, against the milestone in force (rule 3.74).
   Trains are the method, not the measure: they merge many ready parts at once so that fewer
   CI runs are needed. Every ready part rides; a part that fails is dropped from the train and
   fixed outside it. Report train attempts, CI runs per merged item, and how many attempts
   failed on a part defect. A train's duration is not the measure.
2. **Fix rounds.** Rounds per open batch by cause (review finding, red CI, rebase, fast
   check). Every fix round counts. After 3 rounds without fewer failures plus HIGH and MED
   findings, the batch is replanned on a stronger model while the writer and the review seat
   stay as the rule in force names them (rule 3.35); the ceiling is 8. A batch past that is a
   finding now, not later.
3. **Merged but not live.** Units, timers and binaries on main that are not installed and
   active on their hosts; env files a unit needs that are missing; items whose done
   criteria name a live run without live evidence. Give the age since merge.
4. **Merged but not closed.** The count of open items whose work is on main, and the keeper's
   refusal class for each group. A class that repeats is a defect in the close path, not a
   queue to work by hand.
5. **Placement.** Runs per host and role against free memory. All writers on one host while
   another host admits is a finding, even when the placement items are done.
6. **Reviews.** The seats actually used per head against the rule in force: one smart seat,
   or two seats of two different families when only weaker models serve. Count seats that
   ran against the rule.
7. **Telemetry alive.** The collectors and watchdogs that would catch a broken feeder,
   lander or close timer are installed, running and recent. A silent watchdog is a finding.

## Data for every decision

- Every proposal, priority, cap, threshold and cause you send names its data and reasoning:
  the source (telemetry query, keeper read, git range, file and line) and the number
  (rule 3.76).
- A proposal or row that states a cause, size or time carries a `Measured:` line with our own
  numbers and their source; `plangraph router ruling-row FILE` refuses it without one.
- Where the data is missing, mark the proposal `EXPERIMENT` with a measurement plan: what is
  measured, for how long, and which result keeps it. Never decide on a number or a cause
  without one of the two.
- A cause of slow or failed work is claimed only from this system's own numbers. A ruling,
  plan row or message that carries neither data nor an EXPERIMENT label is a gap: file it
  against the gate item of rule 3.76.

## Every gap becomes an item, the same turn

- Search the keeper snapshot by topic first; extend an open item rather than add a second
  one. Edit an open unclaimed item's text; ask routing for anything else.
- A new item states Why with the measured numbers and their source, How as numbered steps,
  the checked-against line, a done check by test and by live query, a priority and a gate.
- A failure of your own work is a lesson item too, filed in the same turn.
- Check a claim of a research run, a seat or another session against the source before a
  decision rests on it.
- A fix that only keeps an item open (a guard, an alert) is not the fix; name who deploys or
  runs the work.

## Turning the plan into work

- Ask routing for the priority order in one batch: speed and token waste first when the
  operator says so, then the rest. Re-rank when a measurement changes the order.
- Send the orchestrator concrete batches: 5 to 8 related items, one area each, disjoint
  paths, unclaimed keys only.
- A part that changes how all tests run lands alone.
- A batch passes its own full CI before it joins a train.

## Operator

- Relay every operator answer or order to routing verbatim, with its time, and ask for the
  decision id. Write the answer into the plan once routing has the id.
- Ask the operator only what is his: money, access and secrets, public, irreversible, live,
  taste. One question, with options, a default and the consequence of no answer.
- Report outcomes with exact numbers, ids and times. Say plainly when something is not done.

## Waiting

- Wait with one background command that exits on the condition: done, stalled for a set
  time, or a deadline. Never a polling loop of model turns.
- Name the watcher in the handoff card, so the successor restarts it.

## Done for a change of direction

The change is done when the rule or decision is carried by code or a skill that the agents
read, the code runs on its hosts, the keeper shows its items closed, and the telemetry that
would show a regression reports. Until then it is open work, and this skill applies.
