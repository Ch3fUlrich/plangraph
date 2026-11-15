"""Exact model admission shared by planner and spawner overrides."""
from plangraph.routing_errors import SpawnRefused
from plangraph.retired_models import check_retired


def validate_model(model, *, allow_inactive=False, catalog_rows=None):
    """The resolver catalog and registered legs are the exact admission door."""
    from plangraph.routing_support.resolver import (get_catalog_models, catalog_row_admitted,
                                           OPERATOR_REMOVED_MODELS)
    from plangraph.mimo_policy import check_mimo_path
    from plangraph.planner import legs as _legs   # read at call time: explicit legs are registered too
    if not isinstance(model, str) or not model:
        raise SpawnRefused('model must be a nonempty string')
    check_retired(model)
    check_mimo_path(model)
    rows = get_catalog_models() if catalog_rows is None else catalog_rows
    inactive = {row['id'] for row in rows if not catalog_row_admitted(row)} if not allow_inactive else set()
    allowed = ({row['id'] for row in rows if catalog_row_admitted(row)} |
               {leg.model for leg in (*_legs.DEFAULT_LEGS, *_legs.EXPLICIT_LEGS)}
               ) - inactive - OPERATOR_REMOVED_MODELS
    if model not in allowed:
        raise SpawnRefused('unknown model: ' + str(model))
    return model
