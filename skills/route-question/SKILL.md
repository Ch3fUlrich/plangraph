---
name: route-question
description: Use when an orchestrator or agent asks the router a question, or when the router itself needs an operator decision. It researches, sets a default, asks the operator interactively, and sends the answer back to the asking agent.
---

Source `router/config` in the router data checkout and resolve
`QUESTIONS.md`, `DECISIONS.md` and `docs/` under `ROUTER_HOME`. Use the
keeper item and lane handoff to identify the build L1.

# Route a question (R-router-01, D-005)

0. **Plan graph first (operator D-1984).** Before any question to the operator or a peer, read the keeper item(s) it concerns (`/v1/snapshot`: title, description, done_criteria, depends_on, evidence, decision_text) and every doc they name (design file, docs/plan/*.md, decision tree, operator answer rounds cited by the keeper item). If the answer is there, act on it and cite the item or doc; ask only what the plan truly leaves open, and name what you checked.
1. **Can the router decide?** Tool and library choices, model routing, scheduling and repo structure are the router's to decide (D-004).
   - Yes: decide, log a D-NNN with the evidence, and reply to the asker. Done.
2. **Research first**, cheaply. Web: a Haiku subagent. Code facts: read-only grep, serena or graphify, or the omnigraph MCP. Second opinion: the configured plangraph-spawner MCP.
3. **Write** `QUESTIONS.md` Q-NNN: question, research pointer, options, default, what it blocks, deadline (about 12 h), reversible yes or no.
4. **Ask interactively** (operator order 2026-09-28): AskUserQuestion with the default first, marked "(Recommended)". Batch all open questions into one call (4 or fewer). Heartbeats pause while it is open (D-031), so ask only while the operator is active; otherwise use the digest and one PushNotification if it blocks work.
5. **Answer**:
   - Log it in docs/decisions-log.md (`by operator`), update the matching rule in DECISIONS.md and remove the Q from QUESTIONS.md.
   - SendMessage the answer to the asking agent (and to the build L1 named in the lane handoff if it changes lanes).
   - Commit and push.
6. **Unanswered at the deadline**: if reversible, accept the default and log it. If not, keep asking.
7. **Answer lands in a session that refuses relayed policy**: target sessions refuse operator decisions relayed by agents (OS-16, OS-18). Give the operator the exact one-line text to type in that session, and record it as OS-NN.
