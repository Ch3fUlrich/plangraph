"""Memo upserts preserve start fields and apply all later fields in order."""
from unittest.mock import patch

from plangraph.projector import history as parser


def test_memo_upserts_merge_without_mutating_history():
    fields = {"other": {"data": "original"}}
    started = {"memo": {"fields": fields}}
    events = [(1, "WORKFLOW_EXECUTION_STARTED", {
        "workflowExecutionStartedEventAttributes": started})]
    for eid, updates in [(2, {"plan_key": {"data": "key"}}),
                         (3, {"extra": {"data": "later"}})]:
        events.append((eid, "WORKFLOW_PROPERTIES_MODIFIED", {
            "workflowPropertiesModifiedEventAttributes": {
                "upsertedMemo": {"fields": updates}}}))
    with patch.object(parser, "_events", return_value=events), \
            patch.object(parser, "_plan_key", return_value=None) as plan_key:
        parser.project_history({}, workflow_id="child", run_id="run")
    assert plan_key.call_args.args[0]["memo"]["fields"] == {
        "other": {"data": "original"}, "plan_key": {"data": "key"},
        "extra": {"data": "later"}}
    assert started == {"memo": {"fields": {"other": {"data": "original"}}}}
