The leg's tool mode now applies to the whole review brief, not only the role header and skill:
the native writer's review prompt marks its file-read and test-run instructions with
`[tools-only]`...`[/tools-only]`, and `prompt_input.compose` drops that block for a toolless inline
seat (and only the markers for every other leg), so no tool demand reaches an inline seat from
the caller's brief either.
