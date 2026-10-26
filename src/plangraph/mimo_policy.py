"""Pure, shared Mimo provider-path policy (D-2459/D-2460/D-2913)."""

import re

from plangraph.routing_errors import SpawnRefused

#: D-2913 adds the opencode free-tier path: it is an E3b review seat.
ACCEPTED_MIMO_IDS = ("mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro",
                     "opencode/mimo-v2.6-flash-free")


class MimoPathError(SpawnRefused):
    """A Mimo model was requested through an unapproved path."""


def is_mimo_id(model_id: str) -> bool:
    """Recognize bare and provider-qualified Mimo model names."""
    value = model_id.lower()
    return bool(re.search(r'(?<![\w])mimo(?=[^\w]|[0-9v_]|$)', value))


def check_mimo_path(model_id: str) -> str:
    """Return unchanged IDs, refusing all but the two approved Mimo IDs."""
    if is_mimo_id(model_id) and model_id not in ACCEPTED_MIMO_IDS:
        raise MimoPathError("Mimo requires one of: " + ", ".join(ACCEPTED_MIMO_IDS))
    return model_id


def mimo_path_allowed(model_id: str) -> bool:
    """Non-raising predicate for catalog candidate filters."""
    return not is_mimo_id(model_id) or model_id in ACCEPTED_MIMO_IDS
