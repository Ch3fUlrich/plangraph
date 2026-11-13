"""Round-cap handoff to an L1 through the existing keeper alarm door.

The spawner's token keeps its existing authority. PR closure, planning runs and
slice definitions belong to the L1 that consumes the alarm and routing request.
"""


def request_replan(service, payload, request_id, path):
    from plangraph.keeper.cli_verbs import Session
    from .stall_watchdog import deliver_event

    card = payload['replan_card']
    reason = (f"round_cap replan required: {payload['item']} PR {payload['pr']}; "
              f"{payload['rounds']} writer rounds. L1: read diff and open findings, plan on "
              f"{payload['replan_leg'] or 'strongest available planning leg'}, close PR unmerged, "
              "define slices <=300 lines with findings verbatim. ")
    reason = reason[:350] + f"Request: {request_id}"
    body = dict(card=payload['item'], reason=reason)
    # Preserve caller-supplied execution fencing; never acquire another run's identity.
    for field in ('workflow_id', 'run_id', 'attempt'):
        if field in card:
            body[field] = card[field]
    try:
        Session().call('POST', 'alarms', body=body)
    finally:
        # The durable local outbox carries the complete findings even when the keeper is down.
        deliver_event(service.state, dict(
            rule='round-cap-replan', kind='alert', recipient='routing', item_key=payload['item'],
            run_id=request_id, text=' '.join(reason.split()), replan_required=payload,
            request_path=str(path)))
