"""Pure, shared Mimo provider-path policy (D-2459/D-2460)."""

import re

ACCEPTED_MIMO_IDS = ("mimo/mimo-v2.6-flash", "mimo/mimo-v2.6-pro")


class MimoPathError(ValueError):
    """A Mimo model was requested through an unapproved path."""


def is_mimo_id(model_id: str) -> bool:
    """Recognize bare and provider-qualified Mimo model names."""
    value = model_id.lower()
    return ('mimo' in re.split(r'[/\\\-_.:]', value)
            or bool(re.match(r'^mimo(?:[0-9v_.-]|$)', re.split(r'[/\\]', value)[-1])))


def check_mimo_path(model_id: str) -> str:
    """Return unchanged IDs, refusing all but the two approved Mimo IDs."""
    if is_mimo_id(model_id) and model_id not in ACCEPTED_MIMO_IDS:
        raise MimoPathError("Mimo requires one of: " + ", ".join(ACCEPTED_MIMO_IDS))
    return model_id


def mimo_path_allowed(model_id: str) -> bool:
    """Non-raising predicate for catalog candidate filters."""
    return not is_mimo_id(model_id) or model_id in ACCEPTED_MIMO_IDS
