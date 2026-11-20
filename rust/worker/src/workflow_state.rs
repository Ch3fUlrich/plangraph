/// The `answer` signal, as the inbox sends it
/// (`src/plangraph/inbox/temporal_signal.py`): `{question_id, key, text, source}`.
/// Unknown fields are ignored and `key` and `source` may be missing; the id and
/// the text are required.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AnswerSignal {
    #[serde(deserialize_with = "id_from_int_or_string")]
    pub question_id: i64,
    #[serde(default)]
    pub key: String,
    pub text: String,
    #[serde(default)]
    pub source: String,
}

/// A question id sent as a JSON integer or as a decimal string.
fn id_from_int_or_string<'de, D: serde::Deserializer<'de>>(d: D) -> Result<i64, D::Error> {
    use serde::de::Error;
    match serde_json::Value::deserialize(d)? {
        serde_json::Value::Number(n) => n
            .as_i64()
            .ok_or_else(|| D::Error::custom("question_id is not an integer")),
        serde_json::Value::String(s) => s
            .trim()
            .parse()
            .map_err(|_| D::Error::custom("question_id is not a number")),
        _ => Err(D::Error::custom("question_id must be a number")),
    }
}

/// How a question ended.
enum QuestionStep {
    /// The text the next attempt gets in its prompt.
    Answer(String),
    /// The card is over (parked or cancelled) and the janitor has run.
    Finished(CardOutput),
}

/// What the merge step produced.
#[derive(Debug)]
enum MergeStep {
    /// Main now points at this attempt; the value is the merged sha.
    Merged(String),
    /// Main moved or the attempt would not fast-forward; the value is the new base.
    Moved(String),
    /// The merge itself said no (post-rebase check, confinement, stale fence);
    /// the class goes to the policy, the text is the check tail, the last value
    /// is main's head at that moment (the base the retry must start from).
    Failed(ExitClass, String, String),
    /// The keeper would not grant the merge lock; nothing was merged.
    RetryLater,
}

#[workflow]
#[derive(Default)]
pub struct Card {
    /// Set by the `cancel` signal; raced against every durable timer.
    cancel_requested: bool,
    /// Token handed to `attempt`; the `cancel` signal fires it.
    op: Option<WorkflowCancellationToken>,
    /// Every `answer` signal received, in arrival order.
    answers: Vec<AnswerSignal>,
}

impl Card {
    /// The human answer to question `id`, if one arrived. A `question_timeout`
    /// signal is not an answer: the workflow takes its own default.
    fn answer_text(&self, id: i64) -> Option<String> {
        self.answers
            .iter()
            .find(|a| a.question_id == id && a.source != "question_timeout")
            .map(|a| a.text.clone())
    }

    /// Store a raw `answer` payload. One that does not parse is dropped with a
    /// log line: a bad signal must never fail the workflow task and wedge the
    /// card. An answer for another question is stored but never matches the
    /// wait, so it is not consumed.
    fn accept_answer(&mut self, raw: serde_json::Value) {
        match serde_json::from_value::<AnswerSignal>(raw) {
            Ok(mut a) => {
                // Cap before it enters workflow state and history, not only
                // where it is injected into a prompt.
                a.text = question::cap_text(&a.text);
                if self.answers.len() >= MAX_STORED_ANSWERS {
                    // Keep the newest: the oldest answer is long consumed.
                    eprintln!("answer signal buffer full: dropping the oldest answer");
                    self.answers.remove(0);
                }
                self.answers.push(a);
            }
            Err(e) => eprintln!("answer signal ignored: {e}"),
        }
    }
}
