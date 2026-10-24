# Report schemas

All reports are JSON. Fields marked * are required. Agents return these as their final message
(or via a response schema where the surface supports one); anything else is discarded upstream.

## done report (task agent to phase agent)
```json
{
  "task": "T-12*", "agent": "impl-sonnet-1*", "status": "done|partial|blocked*",
  "summary": "at most 150 words, what changed and why*",
  "files": ["src/a.py", "tests/test_a.py"],
  "commands": [{"cmd": "pytest tests/test_a.py -q", "exit": 0, "result": "4 passed"}],
  "risks": ["optional open risks"], "evidence": ["commit sha"]
}
```

## error record (attached to the task; read on demand)
```json
{
  "task": "T-12*", "agent": "impl-sonnet-1*", "kind": "tool|env|review|timeout*",
  "cmd": "pytest tests/test_a.py -q*", "exit": 1,
  "tail": "last 20 lines of output*", "suspected_cause": "optional"
}
```

## review findings (reviewer to phase agent)
```json
{
  "task": "T-12*", "agent": "rev-haiku-1*", "family": "other-than-implementer*",
  "lens": "correctness|risk|simplicity*", "verdict": "pass|changes*",
  "findings": [{"severity": "blocker|major|minor", "file": "src/a.py", "line": 41, "issue": "..."}]
}
```

## phase digest (phase agent to project agent), at most 200 words
```json
{
  "phase": "W-3*", "kind": "planning|work*", "outcome": "done|replan|escalate*",
  "tasks": {"done": 8, "failed": 1}, "fix_tasks": 1,
  "summary": "at most 200 words*", "decisions_needed": ["..."],
  "pipeline_signals": ["same lint failure x3"]
}
```
