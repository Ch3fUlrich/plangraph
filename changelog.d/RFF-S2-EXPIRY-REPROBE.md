Expired probe failures no longer contribute to a leg's down streak. A fresh failure after the prior reset or six-hour evidence window requests another probe rather than reviving stale downtime.
Consolidated S2-S5 follow-up: planner clocks govern temporary operator stops consistently; a recovered leg has no disabled skip. Provider evidence and permanent exclusions remain separate, with stale probe failures unable to complete a fresh down streak.

The implement-key lead-writer pin is removed (D-5092), with concrete free-before-subscription regressions. Operator `expires` values use injectable clocks and finite timestamp validation. Automatic stale-evidence reprobes and D-5113 registrations are integrated, not outstanding. Reprobe expiry is applied only after source/status classification; resolver availability shares the planner's limited/down/expired predicate.

Historical S2 local probes recorded console-meta Muse passing and opencode-go Muse failing (exit 7) through the existing evidence recorder. These are availability observations, not performance measurements.
