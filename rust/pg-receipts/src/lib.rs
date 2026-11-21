//! Pure reference model. Each successful verb represents one serialized keeper transaction.

mod ledger;
mod transition;

pub use ledger::Ledger;

pub type Id = u32;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash)]
pub enum State {
    Planned,
    Pending,
    Dispatching,
    Admitted,
    Ok,
    Failed,
    Unknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub enum Class {
    Gated,
    Launch,
    Foreign,
    Budgeted,
    Protected { authority_at_destination: bool },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub struct Spec {
    pub class: Class,
    pub conflict: Option<Id>,
    pub binding: Option<Id>,
    pub credential: Id,
    pub resends: u32,
    pub retryable: bool,
}

impl Spec {
    pub const fn new(class: Class, conflict: Id) -> Self {
        Self {
            class,
            conflict: Some(conflict),
            binding: None,
            credential: 0,
            resends: 0,
            retryable: false,
        }
    }
}

/// Current owner generation and attempt number.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub struct Stamp(pub u32, pub u32);

/// Permission created by exactly one dispatch, valid at the destination for one attempt.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub struct Ticket {
    id: Id,
    stamp: Stamp,
    deadline: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Proof {
    Present,
    AbsentNow,
    FencedAbsent,
    Undecidable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Step {
    Bind(Id),
    Activate,
    Dispatch,
    Expire,
    Finish(bool),
    Observe(Proof),
    Resolve(Id),
    RetryChild(Id),
    RequestCancel(Id),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Reply {
    State(State),
    Send(Ticket),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Refused {
    Missing,
    Conflict,
    Stale,
    Guard,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
enum Delivery {
    InFlight,
    Reserved,
    Applied,
    Fenced,
}

#[cfg(test)]
mod interleavings;
