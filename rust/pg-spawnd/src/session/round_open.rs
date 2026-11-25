//! The open-wait questions of a read transcript: which task a call names, which waits may
//! authorise a stop, what still runs, and how a closed round's open gap is booked.

use super::{
    round_reader::{OpenTool, Prev, Reader, Running, EPISODE_GAP_MS},
    round_time::{Class, OpenWait},
    round_wait::command_reads,
};

impl Reader {
    /// The running task a wait call is bound to: by the structured task id of a
    /// task-output call, or by the task's full output path as a word of the command.
    pub(super) fn named_task(&self, tool: &OpenTool) -> Option<usize> {
        if let Some(id) = &tool.task_ref {
            return self.background.iter().position(|bg| &bg.task.id == id);
        }
        self.background.iter().position(|bg| {
            bg.task
                .output_path
                .as_deref()
                .is_some_and(|path| command_reads(&tool.command, path))
        })
    }
    /// The one task a blocking task-output call names. A shell command that reads a
    /// task's output file is a wait for telemetry only: nothing proves it waits on it.
    fn authorized_task(&self, tool: &OpenTool) -> Option<usize> {
        let id = tool.task_ref.as_ref().filter(|_| tool.structured)?;
        self.background.iter().position(|bg| &bg.task.id == id)
    }
    /// The exit code the harness reported when the task finished, if it did.
    pub fn exit_of(&self, task_id: &str) -> Option<i64> {
        self.exits.get(task_id).copied()
    }
    /// Background tasks that have started and not yet been reported finished.
    pub fn running(&self) -> Vec<Running> {
        let Some(row) = self.rows.last() else {
            return Vec::new();
        };
        self.background
            .iter()
            .map(|bg| Running {
                round_key: row.round_key.clone(),
                task: bg.task.clone(),
                command: bg.command.clone(),
            })
            .collect()
    }
    /// The wait the writer is in at `now_ms`: the oldest open wait call bound to one
    /// running task. Its age is the age of that call alone; idle time, an earlier call
    /// or a sleep that names no task does not count.
    pub fn open_wait(&self, now_ms: i64) -> Option<OpenWait> {
        let row = self.rows.last()?;
        let mut calls: Vec<(&String, &OpenTool)> = self
            .open
            .iter()
            .filter(|(_, tool)| tool.class == Class::Wait)
            .collect();
        calls.sort_by_key(|(id, _)| id.as_str());
        let (call_id, tool, index) = calls
            .into_iter()
            .find_map(|(id, tool)| Some((id.clone(), tool, self.authorized_task(tool)?)))?;
        let bg = &self.background[index];
        // The wait is the episode this call belongs to, named by its first call.
        let episode = self.episodes.get(&bg.task.id);
        Some(OpenWait {
            round_key: row.round_key.clone(),
            task_id: bg.task.id.clone(),
            task: bg.task.clone(),
            call_id: episode.map_or(call_id, |e| e.first_call.clone()),
            command: bg.command.clone(),
            class: bg.class,
            waited_ms: (episode.map_or(0, |e| e.blocked_ms) + (now_ms - tool.started_ms)).max(0),
        })
    }

    /// True while the episode of `call_id` on the task goes on: the writer is between two of
    /// its back-to-back calls (the last line is seconds old) and the command still runs.
    pub fn episode_alive(&self, task_id: &str, call_id: &str, now_ms: i64) -> bool {
        self.background.iter().any(|bg| bg.task.id == task_id)
            && self.episodes.get(task_id).is_some_and(|e| {
                !e.interrupted
                    && e.first_call == call_id
                    && now_ms - e.last_end_ms <= EPISODE_GAP_MS
            })
    }
    /// A round the watchdog closed ends at its terminal time: the open wait that earned
    /// the close is booked up to it, so the row holds the minutes that triggered it.
    pub(super) fn book_closed_tail(&mut self) {
        let Some(at) = self
            .rows
            .last()
            .and_then(|row| self.closed.get(&row.round_key))
            .copied()
        else {
            return;
        };
        let Some(previous) = self.prev_ts.filter(|p| *p < at) else {
            return;
        };
        let gap = at - previous;
        let cause = if !self.open.is_empty() {
            self.tool_cause(gap)
        } else {
            self.cause_of_gap(Prev::Other, None, false, gap)
        };
        self.book(cause, gap);
        self.prev_ts = Some(at);
    }
}
