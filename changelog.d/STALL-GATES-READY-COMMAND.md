The context guard's HANDOFF-NOW and HARD-CAP lines now carry the absolute ready
command (`python3 <guard path> ready <handoff>`), and the fleet-handoff skill names
it the same way. The session wrapper called with `ready` or `status` exits 2 and
names the guard command instead of exiting 0 without a marker.
