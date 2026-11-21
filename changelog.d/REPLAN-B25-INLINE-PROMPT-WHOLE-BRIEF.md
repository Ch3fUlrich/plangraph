The leg's tool mode now applies to the whole review brief, not only the role header and skill:
the native writer's review prompt starts with its file-read and test-run instructions between
`[tools-only]`...`[/tools-only]`, and `prompt_input.compose` drops that leading block (and the review skill, in whichever version
the writer embedded, up to the writer brief) for a toolless inline seat, so no tool demand reaches it from the caller's brief
either. Only a block at the very start counts: marker text in item criteria or a quoted finding stays. Other legs keep the
block once, ahead of the skill, and composing twice changes nothing.
