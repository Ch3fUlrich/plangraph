"""D-3605 (a/F1/F2): dispatch-refusal detection window and quiet-confirmation gate, shared with run_watchdog."""
import asyncio
import re
import time

REFUSAL_WINDOW_S = 30.0      # a refused dispatch speaks at once; later output is task text
REFUSAL_MAX_LINES = 40       # banner / '\r' spinner frames may precede the refusal line (F1)
REFUSAL_MAX_FRAMES = 400     # F1: '\r' spinner frames have their own cap, never the 40-line budget
CONFIRM_QUIET_S = 3.0        # F2: a refusal is booked only after this long with no further activity line
LATE_QUIET_S = 300.0         # REMOVE-AT-R10 (D-3769): an upstream limit line followed by this much silence ends the run
_UPSTREAM = re.compile(r'\s*(?:error:|HTTP \d{3}:).*(?:cooling down|\b(?:status|code)[ =:]*429\b)', re.I)   # REMOVE-AT-R10 (D-3769): ends a run, never books a leg down


class RefusalGate:
    """F2: a found refusal is a pending candidate; later activity cancels it, a quiet period books it."""

    def __init__(self, refused, job, scrub):
        self.refused, self.job, self.scrub, self.pending, self.late = refused, job, scrub, None, None   # REMOVE-AT-R10 (D-3769)

    def candidate(self, text, now):
        if not self.refused.is_set():
            self.pending = (text, now)

    def scan_late(self, lines, now):   # REMOVE-AT-R10 (D-3769)
        """An upstream limit line in this output chunk arms the late booking; a later chunk without one disarms it."""
        from .env_redact import early_refusal, normalize
        text = normalize(''.join(lines))
        up = next((line.strip() for line in text.splitlines() if _UPSTREAM.match(line)), None)
        found = not self.refused.is_set() and (early_refusal(text) or up)
        self.late = (found, now) if found else None

    def progress(self, when):   # REMOVE-AT-R10 (D-3769)
        """Worktree edits are progress too: an edit after the limit line disarms the late booking."""
        if self.late and when > self.late[1]:
            self.late = None

    def activity(self, now):
        self.pending = self.late = None   # called only for activity lines after the candidate line; REMOVE-AT-R10 (D-3769)

    def _expired(self):
        # F2: a candidate whose job-relative time passed the window is task
        # text, never a refusal; dropped in poll() and finish(), never booked.
        return self.pending and self.pending[1] >= self.job.start_time + REFUSAL_WINDOW_S

    def poll(self, now):
        if self._expired():
            self.pending = None
        booked = (self.pending if self.pending and now - self.pending[1] >= CONFIRM_QUIET_S else   # REMOVE-AT-R10 (D-3769)
                  self.late if self.late and now - self.late[1] >= LATE_QUIET_S else None)
        if booked:
            self.job.refusal = self.scrub(booked[0])   # only the matched line is scrubbed
            self.pending = self.late = None   # REMOVE-AT-R10 (D-3769)
            self.refused.set()

    async def finish(self, runner, job_id, proc, result):
        if self._expired():
            self.pending = None   # F2: the window closed: dropped, never booked
        if proc.returncode and self.pending:   # exited non-zero with a pending candidate: book it at once
            self.poll(float('inf'))
            if self.job.status not in ('cancelled', 'stalled', 'timeout'):   # same booking as the watchdog loop
                self.job.status, self.job.exit_code = 'failed', 7
                self.job.events.append({'event': 'dispatch_refused', 'reason': self.job.refusal})
                # F4: reap the remaining process tree as for quiet-confirmed refusals
                await runner._reap_process(job_id, proc)
        return result

    async def run(self):
        while True:
            await asyncio.sleep(.5)
            self.poll(time.time())
