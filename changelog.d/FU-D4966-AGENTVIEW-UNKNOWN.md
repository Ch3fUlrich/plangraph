Agent inventory now marks hosts unknown with `source_unreadable` when a source directory or state file is missing or unreadable, or the optional OpenCode config exists but cannot be read.
Present but empty source directories still report zero agents; regression tests cover missing sources and permission-denied reads.
Empty run/job directories without a record are skipped as absent (including in-progress creation); other record read failures still make the host unknown. Missing optional configs are absent, while non-NotFound config read errors report `source_unreadable`.
The consumer renders unknown host rows with the recorded reason available as a tooltip.
