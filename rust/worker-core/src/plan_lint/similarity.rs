use std::collections::HashMap;
/// Python difflib.SequenceMatcher(None, a, b).ratio(), including autojunk.
/// Matching uses Unicode codepoints and earliest-a, earliest-b tie breaking.
pub fn sequence_ratio(a: &str, b: &str) -> f64 {
    let (a, b): (Vec<_>, Vec<_>) = (a.chars().collect(), b.chars().collect());
    if a.is_empty() && b.is_empty() {
        return 1.0;
    }
    let mut indices: HashMap<char, Vec<usize>> = HashMap::new();
    for (j, ch) in b.iter().enumerate() {
        indices.entry(*ch).or_default().push(j);
    }
    if b.len() >= 200 {
        indices.retain(|_, js| js.len() <= b.len() / 100 + 1);
    }
    let mut queue = vec![(0, a.len(), 0, b.len())];
    let mut total = 0;
    while let Some((alo, ahi, blo, bhi)) = queue.pop() {
        let (mut ai, mut bj, mut size) = (alo, blo, 0);
        let mut previous = HashMap::new();
        for (i, ch) in a.iter().enumerate().take(ahi).skip(alo) {
            let mut current = HashMap::new();
            if let Some(js) = indices.get(ch) {
                for &j in js.iter().filter(|&&j| j >= blo && j < bhi) {
                    let n = previous.get(&j.wrapping_sub(1)).copied().unwrap_or(0) + 1;
                    current.insert(j, n);
                    if n > size {
                        (ai, bj, size) = (i + 1 - n, j + 1 - n, n);
                    }
                }
            }
            previous = current;
        }
        while ai > alo && bj > blo && a[ai - 1] == b[bj - 1] {
            ai -= 1;
            bj -= 1;
            size += 1;
        }
        while ai + size < ahi && bj + size < bhi && a[ai + size] == b[bj + size] {
            size += 1;
        }
        if size > 0 {
            total += size;
            if alo < ai && blo < bj {
                queue.push((alo, ai, blo, bj));
            }
            if ai + size < ahi && bj + size < bhi {
                queue.push((ai + size, ahi, bj + size, bhi));
            }
        }
    }
    2.0 * total as f64 / (a.len() + b.len()) as f64
}
