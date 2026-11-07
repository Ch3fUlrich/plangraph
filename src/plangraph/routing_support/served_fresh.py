"""Confirm a negative discovery result before refusing host admission."""
import hashlib, json, time


def served_fresh(model, models, config, env, *, probe=None):
    from . import resolver as r
    from .opencode_overlay import credential_names

    def present(listing):
        base = model.lower().split('#', 1)[0]
        listing_ids = {m.lower() for m in listing or ()}
        variants = {m for m in listing_ids if m.startswith(base + '#')}
        return listing is None or (base in {m.split('#', 1)[0] for m in listing_ids}
            and ('#' not in model or not variants or model.lower() in variants))

    if present(models) or probe is not None:
        return present(models)
    try:
        providers = json.loads(config).get('providers', {})
    except (ValueError, AttributeError, TypeError):
        providers = {}
    names = sorted(name for name in credential_names(providers) if env.get(name))
    key = (r.provider_config_digest(config), hashlib.sha256(json.dumps(names).encode()).hexdigest())
    with r._served_lock:
        cached = r._served_cache.get(key)
    if (cached is None
            or time.monotonic() - (cached[0] - (30 if not cached[1] else 300)) <= 5):
        return present(models)
    fresh = r._probe_served_models(config, env=env)
    if fresh is not None and present(fresh):
        with r._served_lock:
            r._served_cache[key] = (time.monotonic() + 300, fresh)
    return fresh is not None and present(fresh)
