PreToolUse can run Bash and context guards through the fail-closed guard
runner. Missing guards, crashes and timeouts block the call and record one
outage per guard until it returns a policy result again.
