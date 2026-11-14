use std::collections::BTreeMap;
pub fn options(
    args: &[String],
    values: &[&str],
    flags: &[&str],
) -> Result<BTreeMap<String, String>, String> {
    let mut out = BTreeMap::new();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        let (name, inline) = arg
            .split_once('=')
            .map_or((arg.as_str(), None), |(k, v)| (k, Some(v)));
        if flags.contains(&name) {
            if inline.is_some() {
                return Err(format!("argument {name}: ignored explicit argument"));
            }
            out.insert(name.into(), "true".into());
        } else if values.contains(&name) {
            let val = inline
                .or_else(|| iter.next().map(String::as_str))
                .ok_or_else(|| format!("argument {name}: expected one argument"))?;
            if inline.is_none()
                && val.starts_with('-')
                && !crate::handoff::re(r"^-\d+(?:\.\d*)?$").is_match(val)
            {
                return Err(format!("argument {name}: expected one argument"));
            }
            out.insert(name.into(), val.into());
        } else {
            return Err(format!("unrecognized arguments: {arg}"));
        }
    }
    Ok(out)
}

pub fn help(name: &str) -> &'static str {
    match name {
        "go-row-gate" => include_str!("usage/go-row-gate-help.txt"),
        "inreview-backfill" => include_str!("usage/inreview-backfill-help.txt"),
        "resume-digest" => include_str!("usage/resume-digest-help.txt"),
        "work-summary" => include_str!("usage/work-summary-help.txt"),
        _ => "",
    }
}
pub fn error(name: &str, message: &str) -> i32 {
    let usage = match name {
        "go-row-gate" => include_str!("usage/go-row-gate-usage.txt"),
        "inreview-backfill" => include_str!("usage/inreview-backfill-usage.txt"),
        "resume-digest" => include_str!("usage/resume-digest-usage.txt"),
        "work-summary" => include_str!("usage/work-summary-usage.txt"),
        _ => "",
    };
    eprintln!("{usage}{name}: error: {message}");
    2
}
