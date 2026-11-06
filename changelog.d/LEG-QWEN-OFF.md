Disable the retained qodercli-qwen3.8-flash review leg after exhausted credits
and unavailable-model failures (D-3950). Re-enable only after one passing probe
seat, with one try per day. Existing disabled-leg filtering excludes it from
routes and seat plans; other legs and role assignments are unchanged.
