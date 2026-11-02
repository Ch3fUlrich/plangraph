"""D-3605 (a/F1): dispatch-refusal detection window constants, shared with run_watchdog."""
REFUSAL_WINDOW_S = 30.0      # a refused dispatch speaks at once; later output is task text
REFUSAL_MAX_LINES = 40       # banner / '\r' spinner frames may precede the refusal line (F1)
