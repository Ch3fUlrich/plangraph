mod card;

use super::{
    paths::{io_text, read_text, Paths},
    policy, settings, Caps, Request,
};
use regex::Regex;
use serde_json::Value;
use std::{
    path::{Path, PathBuf},
    sync::LazyLock,
};

pub(super) struct Restart {
    pub model: String,
    pub effort: String,
    pub caps: Caps,
    pub saved_caps: bool,
    pub card: PathBuf,
    pub handoff: PathBuf,
    pub prompt: String,
}

pub(super) fn prepare(
    request: &Request,
    paths: &Paths,
    active_policy: Option<&Value>,
) -> Result<Option<Restart>, String> {
    let args = &request.args;
    if !args.respawn {
        return Ok(None);
    }
    let Some(card) = card::newest(request, paths)? else {
        return Ok(None);
    };
    let spec = &request.spec;
    let saved_caps = (spec["ctx_guard_source"] == "operator")
        .then(|| Caps::from_value(&spec["ctx_guard"]))
        .flatten()
        .or_else(|| Caps::from_value(&spec["launched_caps"]));
    let caps = saved_caps.unwrap_or_else(|| {
        policy::select_caps(active_policy, &args.role, request.override_cap.as_ref()).caps
    });
    if let Some(guard) = args.guard.filter(|guard| *guard != caps) {
        return Err(format!(
            "--ctx-guard {},{}; expected {},{}",
            guard.handoff, guard.hard, caps.handoff, caps.hard
        ));
    }
    let supplied = Path::new(&args.prompt);
    let supplied =
        read_text(supplied).map_err(|e| format!("prompt file: {}", io_text(&e, supplied)))?;
    let window = settings::effective_window(caps.hard);
    let cwd = Path::new(&args.cwd);
    for named in handoff_paths(&supplied) {
        let path = card::absolute(cwd, &paths.home, named);
        if path != card.handoff {
            return Err(format!(
                "handoff {named}; expected {}",
                card.handoff.display()
            ));
        }
    }
    check_caps(&supplied, caps, &window)?;
    let saved = |key: &str, fallback: &str| {
        spec[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(fallback)
            .to_owned()
    };
    let model = saved("model", &args.model);
    let effort = saved("effort", &args.effort);
    let prompt = format!(
        "Restart from handoff {}. Run `resume-digest` first.\n\nModel: {model}\nEffort: {effort}\nCaps: handoff {}, hard {}.\n",
        card.handoff.display(), caps.handoff, caps.hard
    );
    Ok(Some(Restart {
        model,
        effort,
        caps,
        saved_caps: saved_caps.is_some(),
        card: card.path,
        handoff: card.handoff,
        prompt,
    }))
}

static MD_PATH: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"`([^`\r\n]+\.md)`|\]\(([^)\r\n]+\.md)\)|([^\s`"'<>()\[\],;]+\.md)\b"#).unwrap()
});

fn handoff_paths(text: &str) -> impl Iterator<Item = &str> {
    MD_PATH.captures_iter(text).filter_map(|m| {
        let matched = m.get(0)?;
        let path = (1..=3).find_map(|n| m.get(n))?.as_str();
        let prefix = text[..matched.start()]
            .rsplit('\n')
            .next()?
            .trim_end()
            .to_ascii_lowercase();
        let named = path.to_ascii_lowercase();
        let introduced = [
            "handoff",
            "handoff:",
            "handoff path:",
            "handoff =",
            "handoff path =",
        ]
        .iter()
        .any(|end| prefix.ends_with(end))
            || prefix.rsplit_once('[').is_some_and(|(before, label)| {
                label.trim() == "handoff" || before.trim_end().ends_with("handoff:")
            });
        let handoff = named.split('/').any(|part| part.contains("handoff"));
        ((introduced || handoff) && !named.ends_with(".card.md")).then_some(path)
    })
}

fn check_caps(prompt: &str, caps: Caps, window: &str) -> Result<(), String> {
    static PAIR: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r"(?i)(?:--ctx-guard[\s=]+|\b(?:caps|(?:ctx|context|live)\s+guard)\s*[:=]?\s*)(?:handoff\s+)?([0-9_]+[km]?)\s*[,/]\s*(?:hard\s+)?([0-9_]+[km]?)"
    ).unwrap()
    });
    static SINGLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
        r#"(?ix)
        (AUTOOS_CTX_HANDOFF|AUTOOS_CTX_HARD|CLAUDE_CODE_AUTO_COMPACT_WINDOW|CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS)
            \s*[:=]\s*["']?([0-9][0-9_,]*[km]?)
        |
        \b((?:handoff|hard)\s+(?:cap|threshold)|(?:handoff|hard)\s*[:=]|(?:compact(?:ion)?\s+)?window)
            \s*(?:above\s+|at\s+|is\s+|of\s+|[:=]\s*)?([0-9][0-9_,]*[km]?)
        "#
    ).unwrap()
    });
    for m in PAIR.captures_iter(prompt) {
        if number(&m[1]) != Some(caps.handoff) || number(&m[2]) != Some(caps.hard) {
            let handoff = number(&m[1]).map_or_else(|| m[1].to_owned(), |n| n.to_string());
            let hard = number(&m[2]).map_or_else(|| m[2].to_owned(), |n| n.to_string());
            return Err(format!(
                "caps {handoff},{hard}; expected {},{}",
                caps.handoff, caps.hard
            ));
        }
    }
    for m in SINGLE.captures_iter(prompt) {
        let index = if m.get(1).is_some() { 1 } else { 3 };
        let label = m[index].to_ascii_lowercase();
        let value = m[index + 1].trim_end_matches(',');
        if label.contains("handoff") {
            check_number("handoff cap", value, &caps.handoff.to_string())?;
        } else if label.contains("hard") {
            check_number("hard cap", value, &caps.hard.to_string())?;
        } else {
            check_number("compact window", value, window)?;
        }
    }
    Ok(())
}

fn number(raw: &str) -> Option<u64> {
    let value = raw.replace(['_', ','], "").to_ascii_lowercase();
    let (digits, multiplier) = if let Some(digits) = value.strip_suffix('k') {
        (digits, 1000)
    } else if let Some(digits) = value.strip_suffix('m') {
        (digits, 1_000_000)
    } else {
        (value.as_str(), 1)
    };
    digits.parse::<u64>().ok()?.checked_mul(multiplier)
}

fn check_number(label: &str, found: &str, expected: &str) -> Result<(), String> {
    let found_number = number(found);
    if found == expected || found_number.is_some() && found_number == number(expected) {
        return Ok(());
    }
    let found = found_number.map_or_else(|| found.to_owned(), |n| n.to_string());
    Err(format!("{label} {found}; expected {expected}"))
}
