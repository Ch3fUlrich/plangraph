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
        // Spaces keep every run below the long-blob rule and stop wrapped-line joining,
        // so only the PEM rules (not luck) decide whether a body line is scrubbed.
        text.push_str(&format!("Zk{i:03} {MARK} {MARK} ab\n"));
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
    for size in [7, 13, 16, 29, 50, 8192] {
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

#[test]
fn complete_pem_block_straddling_the_redaction_split_leaves_no_body() {
    let (block, mark) = pem(40);
    // One read holds the whole block, and the 512-byte margin falls inside it.
    let data = ["x\n".repeat(1200), block, "tail\n".repeat(90)].concat();
    let out = tail(data.into_bytes(), 8192, 2000, true);
    assert!(!scrubbed(&out).contains(mark), "{out:?}");
}

#[test]
fn encrypted_key_footer_ends_the_swallow_and_later_output_survives() {
    let (block, mark) = pem(400);
    let block = block.replace("PRIVATE KEY", "ENCRYPTED PRIVATE KEY");
    let data = [block, "visible-after\n".repeat(5)].concat();
    for size in 1..=12 {
        let out = tail(data.clone().into_bytes(), size, 120, true);
        assert!(!scrubbed(&out).contains(mark), "size {size}: {out:?}");
        assert!(out.contains("visible-after"), "size {size}: {out:?}");
    }
}

const END: &str = "PG-CHECK-DONE:1";

#[test]
fn diagnostic_marker_cannot_end_a_swallowed_pem_block() {
    let (block, mark) = pem(40);
    let start = block.find("Zk020").unwrap();
    let data = [&block[..start], "\n", END, "\n", &block[start..], "after\n"].concat();
    for size in [1, 5, 16, 64] {
        let out = tail(data.clone().into_bytes(), size, 200, true);
        assert!(!out.contains(mark), "size {size}: {out:?}");
        assert!(!out.contains(END), "size {size}: {out:?}");
        assert!(out.contains("after"), "size {size}: {out:?}");
    }
}

#[test]
fn diagnostic_marker_respects_the_capture_bound() {
    let data = format!("diagnostic\n{END}\n");
    for max in [0, 1, 5] {
        let expected = &data[data.len() - max..];
        assert_eq!(tail(data.clone().into_bytes(), 8192, max, true), expected);
    }
}

#[test]
fn partial_diagnostic_marker_at_the_swallow_transition_stays_redacted() {
    let (block, _) = pem(40);
    let start = block.find("Zk020").unwrap();
    let mut capture = stall_capture::Capture::new(200, true);
    capture.push(format!("{}\nPG-CHECK-", &block[..start]).as_bytes());
    capture.push(b"DONE:1\n");
    capture.push(format!("{}after\n", &block[start..]).as_bytes());
    assert_eq!(capture.finish(), b"\nafter\n");
}

#[test]
fn an_unterminated_pem_diagnostic_is_redacted_at_the_final_cut() {
    let (block, mark) = pem(30);
    let open = block.split("-----END").next().unwrap();
    let data = ["x\n".repeat(400), open.to_string(), format!("\n{END}\n")].concat();
    let out = tail(data.into_bytes(), 4096, 200, true);
    assert!(!out.contains(mark), "{out:?}");
    assert!(!out.contains(END), "{out:?}");
}

#[test]
fn an_unterminated_pem_block_is_swallowed_until_eof() {
    let (block, mark) = pem(400);
    let open = block.split("-----END").next().unwrap();
    let data = format!("before\n{open}\n{END}\n");
    for size in [5, 16, 4096] {
        let out = tail(data.clone().into_bytes(), size, 200, true);
        assert!(!out.contains(mark), "size {size}: {out:?}");
        assert_eq!(out, "before\n", "size {size}: {out:?}");
    }
}
