"""Direct gateway seats whose subscription metadata already exists in the catalog."""
from plangraph.registry_columns import tag_seed_rows


def gateway_seats(models):
    """Reuse agy's quota/privacy metadata, without enabling the unverified generic route."""
    agy = next(row for row in models if row['model_id'] == 'agy/gemini-3.8-flash-tiered-high')
    rows = tag_seed_rows([dict(
        agy, model_id='omniroute/agy/gemini-3.8-flash-high',
        upstream_id='agy-gemini-3.8-flash', context_tokens=128000,
    )])
    rows[0]['harness'] = 'opencode'
    return rows
