# Local gateway retirement boundary

OmniRoute is the gateway for plangraph. This repository does not contain the
legacy local proxy service, its compose/configuration tree, environment mirror,
or the container launcher named in AO-LITELLM-LOCAL-RETIRE. The repository search
found historical notes, reference-check fixtures, and migrated registry guidance;
those references do not prove that a proxy is installed or receives traffic.

The plangraph prerequisite is available: gateway clients can load their credential
from a private `PLANGRAPH_GATEWAY_KEY_FILE`, independently of a proxy environment
file. Gateway admission refuses an absent URL or credential before starting a leg.

The remaining operations belong to the external application repository and host
configuration. They have not been performed or certified by this package:

1. Record a continuous seven-day zero-traffic observation for the legacy listener,
   with start/end timestamps, observation coverage, and the source of the counters.
   An empty repository search or a single connection snapshot is insufficient.
2. Move the external container launcher's key source to its own private client key
   file or secret environment, then record a successful container smoke run through
   OmniRoute. Keep secret values out of command arguments and evidence.
3. Archive the existing proxy configuration and service definitions for rollback.
   Disable and remove the Linux user-unit autostart and Windows startup entries in
   their owning configuration. Remove the proxy files, mirror helper, and its tests.
4. Update external OpenHands fallback comments while retaining its internal client
   library and `litellm_extra_body` behavior. Server applications remain a separate
   retirement item.
5. Check active proxy-port/unit references in that repository. Preserve historical
   audit and reference-detector fixtures with their explicit historical role.
6. Obtain the required two independent review families and judge, with actual
   evidence from the external change and container smoke run.

Rollback belongs with that external change: restore the archived service and
configuration, restore its previous private credential binding, re-enable only the
previous startup mechanism, and verify its container client. Retain the traffic
observation and failure evidence. No host configuration is changed by this document.
