use serde_json::Value;

/// Object keys merge recursively; lists retain unknown entries and order.
pub fn json(target: &mut Value, desired: &Value) -> Result<(), String> {
    match (target, desired) {
        (Value::Object(target), Value::Object(desired)) => {
            for (key, value) in desired {
                if let Some(existing) = target.get_mut(key) {
                    json(existing, value)?;
                } else {
                    target.insert(key.clone(), value.clone());
                }
            }
        }
        (Value::Array(target), Value::Array(desired)) => {
            for value in desired {
                if !target.contains(value) {
                    target.push(value.clone());
                }
            }
        }
        (target, desired) if target.is_object() || target.is_array() => {
            if target != desired {
                return Err("incompatible target structure".into());
            }
        }
        (target, desired) => *target = desired.clone(),
    }
    Ok(())
}

pub fn hooks(target: &mut Value, desired: &Value, superseded: &[String]) -> Result<(), String> {
    let desired_count = desired
        .as_object()
        .ok_or("manifest hooks must be an object")?
        .values()
        .filter_map(Value::as_array)
        .map(Vec::len)
        .sum::<usize>();
    if !superseded.is_empty() && desired_count != 1 {
        return Err("superseded patterns require one desired command per manifest entry".into());
    }
    let hooks = target
        .as_object_mut()
        .ok_or("settings must be an object")?
        .entry("hooks")
        .or_insert_with(|| serde_json::json!({}));
    let hooks = hooks.as_object_mut().ok_or("hooks must be an object")?;
    for (event, entries) in desired
        .as_object()
        .ok_or("manifest hooks must be an object")?
    {
        let groups = hooks
            .entry(event)
            .or_insert_with(|| serde_json::json!([]))
            .as_array_mut()
            .ok_or("hook event must be an array")?;
        for entry in entries
            .as_array()
            .ok_or("manifest hook event must be an array")?
        {
            let command = entry
                .pointer("/hooks/0/command")
                .and_then(Value::as_str)
                .ok_or("missing hook command")?;
            let mut found = false;
            for group in groups.iter_mut() {
                let handlers = group
                    .get_mut("hooks")
                    .and_then(Value::as_array_mut)
                    .ok_or("invalid hook group")?;
                for handler in handlers {
                    if handler
                        .get("type")
                        .and_then(Value::as_str)
                        .is_some_and(|kind| kind != "command")
                    {
                        continue;
                    }
                    let old = handler
                        .get("command")
                        .and_then(Value::as_str)
                        .ok_or("invalid hook handler")?;
                    if old == command {
                        found = true;
                    } else if superseded.iter().any(|pattern| glob(pattern, old)) {
                        handler["command"] = Value::String(command.into());
                        found = true;
                    }
                }
            }
            if !found {
                groups.push(entry.clone());
            }
        }
    }
    Ok(())
}

fn glob(pattern: &str, value: &str) -> bool {
    let pattern = regex::escape(pattern).replace("\\*", ".*");
    regex::Regex::new(&format!("^{pattern}$")).is_ok_and(|re| re.is_match(value))
}
