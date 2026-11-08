Host admission now shares a RAM-only rule between CLI and overflow, reserving
the registry role ramp above the host memory floor. Overflow ranks by RAM
headroom; generated configuration and readiness no longer enforce run counts.
Missing registry data excludes remote hosts and logs conservative local RAM
defaults. The existing OOM guard is unchanged.
