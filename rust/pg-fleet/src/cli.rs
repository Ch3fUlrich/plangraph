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
            out.insert(name.into(), val.into());
        } else {
            return Err(format!("unrecognized arguments: {arg}"));
        }
    }
    Ok(out)
}
