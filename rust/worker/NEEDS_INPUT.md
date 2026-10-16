# needs_input: question, answer signal, resume

A card can stop and ask a human instead of failing. Nothing here touches keeper auth.

1. **Packet.** The harness prints one line and stops:
   `PG_QUESTION: {"text":..,"default":..|null,"deadline_s":N|null,"risk_tags":[..]}`.
   The last such line wins. A valid packet ends the attempt `needs_input` and rides on
   `AttemptResult.question`. An unparsable one (or empty text) is a `contract_defect`.
   A killed run (stall, timeout) is never a question.
2. **Normalising.** A default only counts with a `deadline_s` and no `risk_tags`
   (`Question::normalized`). The keeper is sent the normalised question, so a
   risk-tagged question is stored without a default.
3. **Ask.** Activity `ask_question` calls `POST /v1/ask_question` (deadline = now + `deadline_s`).
   A 4xx parks the card with alarm `question not recorded`; other failures retry.
4. **Wait.** The workflow waits in workflow time on: signal `answer`
   (the inbox object `{question_id,key,text,source}`, matched on `question_id`),
   `cancel`, or one durable timer (`plan_wait`):
   - default + deadline inside the cap: timer = `deadline_s`;
   - otherwise: timer = the wait cap (24 h, or `CardInput.question_wait_cap_s`).
5. **Outcomes.**
   - answer: the text is appended to the prompt of the next attempt, same leg;
   - timer with default: activity `record_question_timeout` calls
     `POST /v1/answer_question {question_id, source:"question_timeout"}`, the card
     continues with the default. A refusal (the verb needs a router or system role
     token) is logged, raises alarm `question_timeout not recorded`, and the card
     still continues with the default;
   - timer without default (none, no deadline, risk-tagged, or past the cap): park + alarm
     `question unanswered`;
   - cancel: janitor, `cancelled`.
6. **Budget.** The question attempt is reported to the keeper but kept out of the
   retry history, so asking never spends retries. Existing workflow shapes replay
   unchanged: the new path triggers only on `needs_input`.

Fixtures: `needs_input_answered`, `needs_input_timeout` (tests/fixtures, projector/).
