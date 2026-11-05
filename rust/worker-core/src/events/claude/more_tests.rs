use super::tests::*;
use super::*;
use serde_json::json;
#[test]
fn claude_tool_use_start_and_result_end_carry_classes() {
    for flavor in [Flavor::Claude, Flavor::Qodercli] {
        let mut p = parser(flavor);
        for (name, class) in [
            ("Read", ToolClass::Read),
            ("Grep", ToolClass::Read),
            ("Glob", ToolClass::Read),
            ("LS", ToolClass::Read),
            ("Bash", ToolClass::Exec),
            ("Edit", ToolClass::Edit),
            ("MultiEdit", ToolClass::Edit),
            ("Write", ToolClass::Edit),
            ("NotebookEdit", ToolClass::Edit),
        ] {
            assert_eq!(
                kinds(&send(
                    &mut p,
                    json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":name,"name":name}]}})
                )),
                vec![(Kind::Tool, Some((class, Phase::Start)))]
            );
            assert_eq!(
                kinds(&send(
                    &mut p,
                    json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":name}]}})
                )),
                vec![(Kind::Tool, Some((class, Phase::End)))]
            );
        }
        assert_eq!(
            kinds(&send(
                &mut p,
                json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"unknown"}]}})
            )),
            vec![(Kind::Tool, Some((ToolClass::Other, Phase::End)))]
        );
        let mut m = monitor();
        for phase in [Phase::Start, Phase::Start, Phase::End] {
            m.feed(Signal {
                kind: Kind::Tool,
                ts_ms: 10,
                tool: Some((ToolClass::Read, phase)),
            });
        }
        assert_eq!(m.open_tools(), 1);
    }
}
#[test]
fn claude_partial_deltas_are_content() {
    for t in [
        "text_delta",
        "thinking_delta",
        "input_json_delta",
        "signature_delta",
    ] {
        check(
            json!({"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":t}}}),
            Kind::Content,
        );
    }
    assert!(send(&mut parser(Flavor::Claude), json!({"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"tool_use"}}})).is_empty());
}
