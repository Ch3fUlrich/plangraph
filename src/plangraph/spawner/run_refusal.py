"""D-3605 (a/F1/F2): dispatch-refusal detection window and quiet-confirmation gate, shared with run_watchdog."""
import asyncio
import time

REFUSAL_WINDOW_S = 30.0      # a refused dispatch speaks at once; later output is task text
REFUSAL_MAX_LINES = 40       # banner / '\r' spinner frames may precede the refusal line (F1)
CONFIRM_QUIET_S = 3.0        # F2: a refusal is booked only after this long with no further activity line


class RefusalGate:
    """F2: a found refusal is a pending candidate; later activity cancels it, a quiet period books it."""

    def __init__(self, refused, job, scrub):
        self.refused, self.job, self.scrub, self.pending = refused, job, scrub, None

    def candidate(self, text, now):
        if not self.refused.is_set():
            self.pending = (text, now)

    def activity(self, now):
        self.pending = None   # called only for activity lines after the candidate line

    def poll(self, now):
        if self.pending and now - self.pending[1] >= CONFIRM_QUIET_S:
            self.job.refusal = self.scrub(self.pending[0])   # only the matched line is scrubbed
            self.pending = None
            self.refused.set()

    def finish(self, proc, result):
        if proc.returncode:   # exited non-zero with a pending candidate: book it at once
            self.poll(float('inf'))
        return result

    async def run(self):
        while True:
            await asyncio.sleep(.5)
            self.poll(time.time())
