# Inline seats

Read when your role header names tool mode `inline`.

Your role header names your tool mode. With tool mode `inline` you have no file or
shell tools (a tool call you try fails): the diff of the change is quoted in this
prompt, and this file replaces every instruction in `skills/review/SKILL.md` that needs a tool.

- Validated means a quoted diff line with its `file:line`. Do not run or ask for a test
  run; name the CI evidence the brief lists (job names, summary lines) instead, and say
  that you did not run it.
- Open no file and do not use the skill tool. Write `FILES READ: diff only`. The spawner
  uses this declaration to distinguish advisory findings from acceptance evidence.
- Findings need only the quoted diff lines; what you cannot see in the diff goes under
  "Unverified" and never blocks.
- You always give the verdict: the `HEAD:` line and the final `VERDICT:` line. Never
  decline the verdict because you could not run something; judge the diff you can read.

