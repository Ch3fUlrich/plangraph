use super::*;
#[test]
fn finalize_scan_chunk_edge_and_truncation() {
    let secret = ["Bear", "er ", &"z9".repeat(16)].concat();
    assert!(crate::writer_secret::scan_bytes(secret.as_bytes()));
    assert!(!crate::writer_secret::scan_bytes(&secret.as_bytes()[..3]));
    assert!(!crate::writer_secret::scan_bytes(&secret.as_bytes()[3..]));
    let mut content = vec![b' '; CHUNK - 3];
    content.extend(secret.bytes());
    let mut stream = format!("oid blob {}\n", content.len()).into_bytes();
    stream.extend(content);
    stream.push(b'\n');
    assert_eq!(
        batch(&mut &stream[..], b"oid\n"),
        Err(Refusal::SecretInHistory)
    );
    for stream in [
        b"oid blob 9\nshort".as_slice(),
        b"oid blob 1\nx",
        b"oid missing\n",
    ] {
        assert_eq!(batch(&mut &stream[..], b"oid\n"), Err(Refusal::BadBundle));
    }
}
#[test]
fn finalize_scan_oversized_commit_refused_before_body_read() {
    struct UnreadBody;
    impl Read for UnreadBody {
        fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
            panic!("oversized commit body must not be read");
        }
    }
    for size in [MAX_COMMIT_BYTES + 1, usize::MAX] {
        let header = format!("oid commit {size}\n");
        let mut stream = BufReader::new(header.as_bytes().chain(UnreadBody));
        assert_eq!(batch(&mut stream, b"oid\n"), Err(Refusal::BadBundle));
    }
    let mut body = vec![b' '; MAX_COMMIT_BYTES];
    body[0] = b'\n';
    let mut stream = format!("oid commit {}\n", body.len()).into_bytes();
    stream.extend(body);
    stream.push(b'\n');
    assert_eq!(batch(&mut &stream[..], b"oid\n"), Ok(()));
}
#[test]
fn finalize_scan_many_blobs_are_consumed_incrementally() {
    struct Counting {
        left: usize,
        offset: usize,
        largest: usize,
    }
    impl Read for Counting {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.largest = self.largest.max(out.len());
            let object = b"oid blob 4\nsafe\n";
            let mut n = 0;
            while n < out.len() && self.left > 0 {
                out[n] = object[self.offset];
                n += 1;
                self.offset += 1;
                if self.offset == object.len() {
                    self.offset = 0;
                    self.left -= 1;
                }
            }
            Ok(n)
        }
    }
    let mut source = Counting {
        left: 2000,
        offset: 0,
        largest: 0,
    };
    batch(&mut BufReader::new(&mut source), &b"oid\n".repeat(2000)).unwrap();
    assert_eq!(source.left, 0);
    assert!(source.largest <= CHUNK);
}

#[test]
fn finalize_scan_multi_mib_letters_have_linear_carry_and_scan_work() {
    struct Counting {
        remaining: usize,
        largest: usize,
        total: usize,
    }
    impl Read for Counting {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.largest = self.largest.max(out.len());
            let count = out.len().min(self.remaining);
            out[..count].fill(b'q');
            self.remaining -= count;
            self.total += count;
            Ok(count)
        }
    }
    let size = 5 * 1024 * 1024 + 17;
    let mut reader = Counting {
        remaining: size,
        largest: 0,
        total: 0,
    };
    scan_chunks(&mut reader, size, true).unwrap();
    assert_eq!(reader.total, size);
    assert_eq!(reader.remaining, 0);
    assert_eq!(reader.largest, CHUNK);
    let mut scanner = BlobScanner::default();
    for position in (0..size).step_by(CHUNK) {
        let count = CHUNK.min(size - position);
        assert!(!scanner.push(&vec![b'q'; count]));
        assert_eq!(scanner.bytes.len(), position + count);
        assert!(scanner.bytes.capacity() <= 2 * size);
    }
    assert!(!scanner.finish());
    assert!(scanner.scanned_bytes < 3 * size);
}

#[test]
fn unbounded_candidates_survive_chunk_edges_and_encodings() {
    for length in [40 * 1024, 140 * 1024] {
        // A letter-only name is harmless until its credential suffix arrives.
        let secret = format!("{}{}=x", "q".repeat(length), ["TO", "KEN"].concat());
        for encoding in 0usize..5 {
            let mut bytes = vec![b' '; CHUNK - 7];
            match encoding {
                0 => bytes.extend(secret.bytes()),
                _ => {
                    if encoding > 2 {
                        bytes.push(b' ');
                    }
                    for unit in secret.encode_utf16() {
                        bytes.extend(if encoding.is_multiple_of(2) {
                            unit.to_be_bytes()
                        } else {
                            unit.to_le_bytes()
                        });
                    }
                }
            }
            assert!(crate::writer_secret::scan_bytes(&bytes));
            assert_eq!(
                scan_chunks(&mut bytes.as_slice(), bytes.len(), true),
                Err(Refusal::SecretInHistory)
            );
        }
    }
}

fn encode(text: &str, encoding: usize) -> Vec<u8> {
    if encoding == 0 {
        return text.as_bytes().to_vec();
    }
    let mut bytes = vec![b' '; usize::from(encoding > 2)];
    bytes.extend(text.encode_utf16().flat_map(|unit| {
        if encoding.is_multiple_of(2) {
            unit.to_be_bytes()
        } else {
            unit.to_le_bytes()
        }
    }));
    bytes
}

#[test]
fn policy_starts_and_context_survive_every_lane_edge() {
    // Include word, underscore, PEM dash, and generic blob '+'/'/' starts,
    // plus punctuation and whitespace context. Raw retention also preserves
    // any future policy start/lookbehind without a local start-character list.
    let secrets = [
        ["Bear", "er abcdefgh"].concat(),
        ["_API_", "KEY=x"].concat(),
        ["-----BEGIN TEST ", "PRIVATE KEY-----"].concat(),
        format!("+{}", "a1".repeat(22)),
        format!("/{}", "a1".repeat(22)),
        ["ey", "Jabcdefgh.abcdefgh.abcdefgh"].concat(),
        ["gh", "p_", &"a1".repeat(20)].concat(),
        "ab12".repeat(8),
    ];
    for secret in secrets {
        for context in ["\"", "'", "=", ":", "/", " "] {
            for encoding in 0usize..5 {
                let encoded = encode(&format!("{context}{secret}\nclean"), encoding);
                assert!(crate::writer_secret::scan_bytes(&encoded));
                for edge in [CHUNK, 2 * CHUNK, 4 * CHUNK]
                    .into_iter()
                    .flat_map(|checkpoint| checkpoint - 1..=checkpoint + 1)
                {
                    // Place both the start and end around each exact byte edge.
                    for padding in [
                        edge - encode(context, encoding).len(),
                        edge - encode(&format!("{context}{secret}"), encoding).len(),
                    ] {
                        let mut bytes = vec![b' '; padding];
                        bytes.extend_from_slice(&encoded);
                        assert_eq!(
                            scan_chunks(&mut bytes.as_slice(), bytes.len(), true),
                            Err(Refusal::SecretInHistory),
                            "encoding={encoding} edge={edge} padding={padding}"
                        );
                    }
                }
            }
        }
    }
}

#[test]
fn eof_last_byte_completes_secret_in_every_encoding() {
    let secret = ["API_", "KEY=x"].concat();
    for encoding in 0..5 {
        let mut bytes = vec![b' '; CHUNK];
        bytes.extend(encode(&secret, encoding));
        let mut scanner = BlobScanner::default();
        for chunk in bytes.chunks(CHUNK) {
            assert!(!scanner.push(chunk));
        }
        assert_eq!(scanner.scanned, 0, "must exercise EOF, not a checkpoint");
        assert!(scanner.finish(), "encoding={encoding}");
        assert_eq!(
            scan_chunks(&mut bytes.as_slice(), bytes.len(), true),
            Err(Refusal::SecretInHistory)
        );
    }
}
