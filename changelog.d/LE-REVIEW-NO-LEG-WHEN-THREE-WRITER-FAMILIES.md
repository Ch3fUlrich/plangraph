`plangraph review` reads the Writer trailers of the range, groups the commits by writer
family set and runs one review pass per group: its route and seats exclude only that
group's families, and its prompt names the group's commits. The combined verdict is PASS
only when every seat passes; a single-family range still yields one pass. A batch written
by gpt, gemini and claude writers no longer fails with no leg because the whole range
excluded all three families. The plan-order `bin/review.sh` is not in this repository: it
must stop passing every writer family of the range as one exclusion and call this command
(or group by family itself) instead.
