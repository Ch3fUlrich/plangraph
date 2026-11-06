"""HTTP-only snapshot validation; refusals never contain query values."""
from . import fmt, snapshot_filter, timing
from .errors import Refused


def parse(query):
    """Return nonblank S1 keyword arguments from a parse_qs mapping."""
    values = {}
    for name in (*snapshot_filter.PARAMS, "fmt"):
        sent = [value for value in query.get(name, []) if value != ""]
        if len(sent) > 1:
            raise Refused("validation", name)
        if sent:
            values[name] = sent[0]
    filters = {name: value for name, value in values.items() if name != "fmt"}
    for name, value in filters.items():
        try:
            snapshot_filter._validate({name: value})
        except Refused:
            raise Refused("validation", name) from None
    if "open" in filters and "closed" in filters:
        raise Refused("validation", "open closed")
    if filters and values.get("fmt") == "text":
        raise Refused("validation", "fmt")
    return filters


def read(store, principal, query):
    filters = parse(query)
    timing.filtered_read(bool(filters))
    data = store.snapshot(principal, **filters)
    as_text = next((v for v in query.get("fmt", []) if v), None) == "text"
    return (fmt.snapshot_text(data), True) if as_text else (data, False)
