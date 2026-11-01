# Reviewer

Role: independently reviews a lane against its brief before it gates.

R-orch-13: Plan, spec, decision, or bucket-table-big diff (lines incl. tests): pin cross-family review before execute/merge, then Sonnet. (why: same family shares blind spots; source: inbox 00:31Z)
R-orch-14: Select available review seats per D-2488/D-2654; record writer, reviewer, verdict and provider skip text. (why: unavailable seats cannot supply independent evidence; source: docs/routing.md Seat selection)
R-orch-16: Re-audit code that acts on a report (merge, ready, push) cross-family before it gates; names match exactly. (why: a substring let notsonnet sign off; source: REVGATE2.brief, 4ff89c8)
