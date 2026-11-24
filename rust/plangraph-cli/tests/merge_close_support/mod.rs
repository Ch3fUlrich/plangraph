//! A scripted keeper for the merge-close tests: each call must match the next expected body.
use plangraph_cli::feeder::Transport;
use serde_json::Value;
use std::cell::RefCell;
use std::collections::VecDeque;

pub struct Script(RefCell<VecDeque<(Value, Value)>>);

impl Script {
    pub fn new<const N: usize>(steps: [(Value, Value); N]) -> Self {
        Self(RefCell::new(steps.into()))
    }

    pub fn assert_finished(&self) {
        assert!(self.0.borrow().is_empty(), "unconsumed keeper calls");
    }
}

impl Transport for Script {
    fn call(&self, verb: &str, body: Option<Value>) -> Result<Value, String> {
        let (expected, reply) = self
            .0
            .borrow_mut()
            .pop_front()
            .expect("unexpected keeper call");
        assert_eq!(verb, "close_merged_auto");
        assert_eq!(body, Some(expected));
        match reply.as_str() {
            Some(error) => Err(error.into()),
            None => Ok(reply),
        }
    }
}
