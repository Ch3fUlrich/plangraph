# Principle 11 — Telemetry: every path leaves a record that explains it (rigid)

Read when you add or change a command, job, unit, gate, queue or alert.

Work that fails, waits or is refused without a record cannot be found, only
stumbled upon. Every command, job, unit and gate you add writes what an
investigator needs to see **what happened, when, for which item and why**, and
something reads it. Measured 2026-11-20: three finished batches waited 80 minutes
for reviews that could not start and no record said so; the alert list was
rebuilt from every row since October on each call and held 1,191 rows, eight of
them current.

1. **One structured record per run**: what ran, the ids to join on (item key, run
   id, head sha), start, end, duration, outcome, and the reason whenever the
   outcome is not success.
2. **A wait or a queue records since when and why**, and a rule or a digest
   reads it. A stall nobody is told about is a defect of the code that waits.
3. **Store what is asked often; update it on the event that changes it.** A
   count, a status or an alert lives in a table or a state file. A read never
   rebuilds it from the full history.
4. **A refusal names what it looked for and what it found**, so the next step is
   visible without a debugger.
5. **Done includes visible**: the done criteria of an item that adds a job, a
   gate or a unit say how its failure shows up, and a test or a live readback
   proves that it does.

| Red flag | Reality |
|---|---|
| "It logs to the journal" | Free text nobody queries. Write the record with its ids and outcome. |
| "We can recompute it when someone asks" | That is a full pass per question, and the answer comes after the incident. Store it. |
| "It exits non-zero, that is enough" | The caller sees a number. Name the reason and what was expected. |
| "Monitoring is a later item" | Then the first failure is found by a person asking why nothing moved. |
