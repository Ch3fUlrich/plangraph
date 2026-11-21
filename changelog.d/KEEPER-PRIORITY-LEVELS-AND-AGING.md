The keeper holds the priority scheme: an `LE-` item created without a priority starts at 8, a new
INCIDENT LOG line on it raises it one level (max 10), and the reaper drops an open, unclaimed 10
(after 3 days) or 9 (after 7 days) one level with a `priority_set` event carrying the reason; a
re-confirmed priority restarts the clock and a claim stops it. `digest` prints the open count per
level and warns when level 10 holds more open items than the lanes closed in the last 7 days.
