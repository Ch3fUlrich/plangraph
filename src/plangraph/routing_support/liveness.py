"""Shared route exclusion predicate for quota and liveness evidence."""


def unavailable(state):
    return state['state'] in ('limited', 'down')
