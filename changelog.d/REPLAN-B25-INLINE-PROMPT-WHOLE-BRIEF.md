The leg's tool mode now applies to the whole review brief, not only the role header and skill:
the native writer's review prompt marks its file-read and test-run instructions with
`[tools-only]`...`[/tools-only]`, and `prompt_input.compose` drops that block for a toolless inline
seat (and only the markers for every other leg), so no tool demand reaches an inline seat from
the caller's brief either.
The block is honoured only as the first thing after the leading review skill (any version of it, recognised by its front
matter) and before the writer brief; the native prompt puts it there, so marker text in item criteria or a quoted finding stays.
