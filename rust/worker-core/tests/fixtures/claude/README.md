Sanitized captures, 2026-11-05: real binaries, read-a-file and one-word prompts on model claude-haiku-5-5 and Qwen3.8-Flash.
All ids are synthetic and all free text is replaced; only event shapes, kinds, tool names and arrival times are real.
Each .arrival file holds one line: milliseconds since spawn at which each stdout line arrived, same count as the jsonl lines.
Runs were short, so the largest gap measures these runs only; design decision: silence 180 s only when it is under 90 s.
claude_version: 2.1.318
qodercli_version: 1.1.69
largest_gap_ms: 2204
silence_s: 180
qodercli_stream: live
