# needs_input: question, answer signal, resume

A card can stop and ask a human instead of failing. Nothing here touches keeper auth.

1. **Packet.** The harness prints one line and stops:
   `PG_QUESTION: {"text":..,"default":..|null,"deadline_s":N|null,"risk_tags":[..]}`.
   The last such line wins. A valid packet ends the attempt `needs_input` and rides on
   `AttemptResult.question`. An unparsable one (or empty text) is a `contract_defect`.
   A killed run (stall, timeout) is never a question.
2. **Normalising.** A default only counts with a `deadline_s` and no `risk_tags`
   (`Question::normalized`). The keeper is sent the normalised question, so a
   risk-tagged question is stored without a default. Explicitly: a default of `""`
   (or only blanks) is "no default"; `deadline_s: 0` is "no deadline" (so the
   default cannot be taken and the card waits to the cap). Question text, default
   and every answer are untrusted: each is cut to 4000 characters and ends in
   `[... truncated]` when it was longer (`question::cap_text`), before it is
   stored or put into a prompt.
3. **Ask.** Activity `ask_question` calls `POST /v1/ask_question` (deadline = now + `deadline_s`).
   Only 401, 403, 409 and 422 are permanent refusals: they park the card with alarm
   `question not recorded`. A transport error, 408, 429 or 5xx is transient: retried
   in the activity (250 ms, 1 s, 3 s), then left to the activity retry policy. Any
   other status is not retried in the activity.
4. **Wait.** The workflow waits in workflow time on: signal `answer`
   (the inbox object `{question_id,key,text,source}`, matched on `question_id`),
   `cancel`, or one durable timer (`plan_wait`). The signal payload is read
   tolerantly: extra fields and a missing `key`/`source` are fine, `question_id`
   may be an integer or a numeric string; a payload that does not parse is
   dropped with a log line (it never fails the workflow task), and one for another
   `question_id` is kept but not consumed:
   - default + deadline inside the cap: timer = `deadline_s`;
   - otherwise: timer = the wait cap (24 h, or `CardInput.question_wait_cap_s`).
5. **Outcomes.**
   - answer: the text is appended to the prompt of the next attempt, same leg;
   - timer with default: activity `record_question_timeout` calls
     `POST /v1/answer_question {question_id, source:"question_timeout"}`, the card
     continues with the default. A refusal (the verb needs a router or system role
     token) or an outage that outlasts the transient retries is logged, raises alarm
     `question_timeout not recorded`, and the card still continues with the default
     (this activity never fails). Alarms are best effort everywhere: an alarm that
     cannot be delivered after 3 tries is dropped, never a workflow failure;
   - timer without default (none, no deadline, risk-tagged, or past the cap): park + alarm
     `question_timeout` (the park packet's check tail is `question_timeout: <question text>`);
   - cancel: janitor, `cancelled`.
6. **Budget.** The question attempt is reported to the keeper but kept out of the
   retry history, so asking never spends retries. What bounds a card that keeps
   asking is `max_question_rounds` (default 3, `Config`; `CardInput.max_question_rounds`
   overrides it for tests): once that many questions were answered, the next
   `needs_input` parks the card with alarm `question rounds exhausted` without
   asking the keeper. Each answered round is a new attempt (next `n`, new worktree). Existing workflow shapes replay
   unchanged: the new path triggers only on `needs_input`.

Fixtures: `needs_input_answered`, `needs_input_timeout` (tests/fixtures, projector/).
