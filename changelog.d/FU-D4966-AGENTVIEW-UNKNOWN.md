Agent inventory now marks hosts unknown with `source_unreadable` when a source directory or configured OpenCode state file is missing or unreadable.
Present but empty source directories still report zero agents; regression tests cover missing sources and permission-denied reads.
