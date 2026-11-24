The landing train chain check (push gate and replay) accepts the merge of the new
`origin/main` into an open train, so a second train no longer has to be rebuilt from scratch
after the first one landed. The remerge is recomputed on replay, refused when it conflicts or
carries an edit, and recorded as `remerges` in the review artifact. Every other second parent
is still refused. The train base is read from the first-parent chain instead of the merge-base.
