Fixed the D-6759 re-review findings of the UI auto deploy: the render unit now resolves an unset `PLANGRAPH_STATE` to the
default `live_follow` uses instead of failing, `export` replaces a broken symlink at the render-tree path, the README
describes the verified-export flow, and two open-PR gate tests use runs that pass every gate filter so they can fail.
