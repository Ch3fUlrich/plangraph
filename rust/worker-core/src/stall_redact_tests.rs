//! A secret that straddles the capture cut must never survive as a fragment.
use super::*;

/// A token-shaped fake secret assembled from fragments (no token-shaped literal in source).
fn secret() -> String {
    ["gh", "p_", &"Qr7x".repeat(10)].concat()
}

/// Hands out its data in fixed-size chunks, like a pipe read.
struct Chunks {
    data: Vec<u8>,
    at: usize,
    size: usize,
}
impl Read for Chunks {
    fn read(&mut self, out: &mut [u8]) -> io::Result<usize> {
        let n = self.size.min(out.len()).min(self.data.len() - self.at);
        out[..n].copy_from_slice(&self.data[self.at..self.at + n]);
        self.at += n;
        Ok(n)
    }
}

fn tail(data: Vec<u8>, size: usize, max: usize, redact: bool) -> String {
    let last = AtomicU64::new(0);
    let reader = Chunks { data, at: 0, size };
    String::from_utf8_lossy(&read_tail(reader, Instant::now(), &last, max, redact)).into_owned()
}

fn fragment(secret: &str) -> &str {
    &secret[12..24]
}

#[test]
fn final_cut_through_a_secret_leaves_no_fragment() {
    let s = secret();
    let data = [" ".repeat(20), s.clone(), " ".repeat(5)]
        .concat()
        .into_bytes();
    let cut_inside = data.len() - 35;
    assert!(cut_inside > 20 && cut_inside < 20 + s.len());
    let raw = tail(data.clone(), 4096, 35, false);
    assert!(
        raw.contains(fragment(&s)),
        "the unredacted capture keeps the fragment: {raw:?}"
    );
    let out = tail(data, 4096, 35, true);
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived the cut: {out:?}"
    );
}

#[test]
fn midstream_drain_through_a_secret_leaves_no_fragment() {
    let s = secret();
    // 16-byte chunks: the front is drained again and again while the secret streams in.
    let mut data = " ".repeat(30).into_bytes();
    data.extend_from_slice(s.as_bytes());
    data.extend_from_slice(" ".repeat(70).as_bytes());
    let out = tail(data.clone(), 16, 40, true);
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived a mid-stream drain: {out:?}"
    );
    assert!(!tail(data, 16, 40, false).is_empty());
}

#[test]
fn redacting_capture_keeps_plain_text_and_a_split_utf8_tail() {
    let data = ["plain output ", "é"].concat().into_bytes();
    // Chunk size 1 splits the two-byte character; the tail must stay intact text.
    assert_eq!(tail(data, 1, 1 << 20, true), "plain output é");
}

#[test]
fn supervise_redacts_before_the_capture_limit_cuts() {
    let s = secret();
    let script = format!("printf '%s' '{}{}{}'", " ".repeat(20), s, " ".repeat(5));
    let mut cmd = Command::new("sh");
    cmd.arg("-c").arg(script);
    let opts = Opts {
        max_output: 35,
        redact_discarded: true,
        grace: Duration::from_millis(300),
        ..Opts::default()
    };
    let out = String::from_utf8_lossy(&supervise(cmd, &opts).unwrap().output).into_owned();
    assert!(
        !out.contains(fragment(&s)),
        "fragment survived supervision: {out:?}"
    );
}

/// A PEM block whose body lines carry one distinctive fragment each.
fn pem(lines: usize) -> (String, &'static str) {
    const MARK: &str = "Qm7xLp3Rt9Vw1Yc5Nb8";
    let mut text = ["-----BEGIN ", "PRIVATE", " KEY-----\n"].concat();
    for i in 0..lines {
        // 64-character lines, as PEM wraps them: a read boundary inside a line leaves
        // two halves that are each too short for the long-blob rule.
        text.push_str(&format!("Zk{i:03}{MARK}{MARK}{MARK}ab\n"));
    }
    text.push_str(&["-----END ", "PRIVATE", " KEY-----\n"].concat());
    (text, MARK)
}

fn scrubbed(out: &str) -> String {
    crate::redact::redact(out)
}

#[test]
fn pem_block_split_across_midstream_drains_leaves_no_body_line() {
    let (block, mark) = pem(40);
    let data = [" filler\n".repeat(60), block, "tail\n".repeat(60)].concat();
    // Small reads and a small capture: the drains land inside the block.
    for size in [7, 13, 16, 29, 50] {
        let out = tail(data.clone().into_bytes(), size, 300, true);
        assert!(!scrubbed(&out).contains(mark), "size {size}: {out:?}");
        assert!(!out.contains("Zk0"), "size {size}: raw body kept: {out:?}");
    }
}

#[test]
fn pem_block_longer_than_the_capture_is_swallowed_to_its_footer() {
    let (block, mark) = pem(400);
    let data = ["head\n".repeat(20), block, "after\n".repeat(20)].concat();
    for size in [5, 16, 100] {
        let out = tail(data.clone().into_bytes(), size, 200, true);
        assert!(!scrubbed(&out).contains(mark), "size {size}: {out:?}");
        assert!(
            out.contains("after"),
            "output after the footer is kept: {out:?}"
        );
    }
}

#[test]
fn footer_split_across_reads_still_ends_the_swallow() {
    let (block, mark) = pem(400);
    let data = [block, "visible-after\n".repeat(5)].concat();
    // Every split position of the footer line is covered by sweeping the read size.
    for size in 1..=9 {
        let out = tail(data.clone().into_bytes(), size, 120, true);
        assert!(!scrubbed(&out).contains(mark), "size {size}: {out:?}");
        assert!(out.contains("visible-after"), "size {size}: {out:?}");
    }
}

#[test]
fn pem_block_open_at_a_drain_stays_whole_until_its_footer_arrives() {
    let (block, mark) = pem(40);
    // The block starts just before the drain point and ends after it, and it is
    // far smaller than the capture: only its unfinished end must stay raw.
    let data = ["x\n".repeat(1750), block, "tail\n".repeat(400)].concat();
    let out = tail(data.into_bytes(), 64, 2000, true);
    assert!(!scrubbed(&out).contains(mark), "{out:?}");
}
