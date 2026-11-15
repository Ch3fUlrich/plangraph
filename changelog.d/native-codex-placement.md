Native Codex writer cards now use spawner RAM admission and overflow placement,
with target-host pushed-base worktrees, disk floors, same-session review resumes,
final fast gates, and host/base/head/review/exit evidence in the run record.

Remote native result/cancel receipts now tolerate shell banners through nonce framing. Placement can select a ready remote when the controller's checkout is unavailable, preserving readiness refusal reasons. The native MCP bridge validates unsupported options, preserves prompts beginning with a dash, and returns adapter refusals as structured errors.

Native worktrees live under each run’s state directory so retained runs leave the source checkout clean for future worktree admission. Disk probes follow the worktree filesystem.

Native cards require an explicit absolute repository before admission. Result reads remain local; CLI and MCP polls refresh remote receipts off the event loop, and Python cancellation makes one remote round trip. CLI wait refreshes remote native runs at most once per second within its timeout.
