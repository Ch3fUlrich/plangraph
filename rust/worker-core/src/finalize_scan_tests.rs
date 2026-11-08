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
fn finalize_scan_multi_mib_buffer_is_bounded() {
    struct Counting {
        remaining: usize,
        largest: usize,
        total: usize,
    }
    impl Read for Counting {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.largest = self.largest.max(out.len());
            let count = out.len().min(self.remaining);
            out[..count].fill(b' ');
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
        assert!(!scanner.push(&vec![b' '; CHUNK], position));
        assert!(scanner.text.iter().all(|text| text.len() <= 1));
        assert!(scanner.text.iter().all(|text| text.capacity() <= 8));
    }
}

#[test]
fn unbounded_candidates_survive_chunk_edges_and_encodings() {
    for length in [40 * 1024, 140 * 1024] {
        // A letter-only name is harmless until its credential suffix arrives.
        let secret = format!("{}{}=x", "q".repeat(length), ["TO", "KEN"].concat());
        for encoding in 0..5 {
            let mut bytes = vec![b' '; CHUNK - 7];
            match encoding {
                0 => bytes.extend(secret.bytes()),
                _ => {
                    if encoding > 2 {
                        bytes.push(b' ');
                    }
                    for unit in secret.encode_utf16() {
                        bytes.extend(if encoding % 2 == 0 {
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

#[test]
fn secret_starts_or_ends_exactly_at_chunk_edge() {
    let secret = ["Bear", "er abcdefgh"].concat();
    for padding in [CHUNK, CHUNK - secret.len()] {
        let mut bytes = vec![b' '; padding];
        bytes.extend(secret.bytes());
        bytes.extend_from_slice(b"\nclean");
        assert_eq!(
            scan_chunks(&mut bytes.as_slice(), bytes.len(), true),
            Err(Refusal::SecretInHistory)
        );
    }
}
