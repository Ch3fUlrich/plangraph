The public scrub's ipv6 rule now allows the loopback address and the RFC 3849 documentation
range (`allow_ipv6` in `tools/split/scrub_check.py`, parsed with `ipaddress`), as the ipv4
rule already allows 127. and the TEST-NET ranges. Rules files may extend the list; a global
address is still flagged. The per-value hash allowances for host_command.rs this item named
are not in this tree.
