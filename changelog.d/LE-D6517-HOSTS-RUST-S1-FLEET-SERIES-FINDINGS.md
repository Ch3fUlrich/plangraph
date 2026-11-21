Fixed the D-6517 findings of the fleet series: reading a host history opens it without
blocking and refuses symlinks and non-regular files, so a terminal run transition cannot hang
on a FIFO, and the per-run counter delta is unknown unless a distinct sample was taken during
the run.
