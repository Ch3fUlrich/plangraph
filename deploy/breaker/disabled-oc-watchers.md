# Retired watcher drop-ins

At operator cutover remove `oc-l0-watch.service.d/breaker-exec.conf` and
`oc-pilot-watch.service.d/breaker-exec.conf`. Both watchers stay disabled.
The retained ctx-watchdog wrapper is `ctx-watchdog.conf.in`, rendered using the
same pinned router path as the main service. It uses plangraph state only.

The external routerctl UNIT_BREAKER probe must call
`plangraph unit-breaker status` instead of the AutoOS Python tool.
No router checkout is owned or modified by this repository.
