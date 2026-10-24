#!/usr/bin/env bash
# Handshake check: throwaway CA + openssl mTLS server (no gRPC). Asserts that the spike driver
# presents a client cert the server verifies, and that a wrong CA or a wrong server name is refused.
# Build first: cargo build --bins in rust/spikes/temporal-spike. Needs openssl; Linux/WSL.
set -u
T=$(mktemp -d); cd "$T"; S=
trap '[ -n "$S" ] && kill $S 2>/dev/null; rm -rf "$T"' EXIT
BIN=${CARGO_TARGET_DIR:-target}/debug/driver
fail() { echo "FAIL: $*"; exit 1; }
mkca() { openssl req -x509 -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.crt" -days 1 -subj "/CN=$1" 2>/dev/null; }
mkleaf() { # name ca
 openssl req -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=$1" 2>/dev/null
 printf "subjectAltName=DNS:temporal.test\nextendedKeyUsage=serverAuth,clientAuth\n" > "$1.ext"
 openssl x509 -req -in "$1.csr" -CA "$2.crt" -CAkey "$2.key" -CAcreateserial -out "$1.crt" -days 1 -extfile "$1.ext" 2>/dev/null
}
mkca ca; mkca otherca; mkleaf server ca; mkleaf client ca
openssl s_server -accept 17999 -cert server.crt -key server.key -CAfile ca.crt -Verify 1 -alpn h2 -quiet > srv.log 2>&1 &
S=$!; sleep 1
run() { # ca server_name -> sets nothing; server log is reset each time
 : > srv.log
 SPIKE_ADDR=https://127.0.0.1:17999 TEMPORAL_TLS_CERT=client.crt TEMPORAL_TLS_KEY=client.key \
  TEMPORAL_TLS_CA=$1 TEMPORAL_TLS_SERVER_NAME=$2 timeout 20 "$BIN" > out.log 2>&1
 sleep 1
}
run ca.crt temporal.test
grep -aq "depth=0 CN = client" srv.log || fail "server did not see the client cert (positive case)"
echo "ok: client cert presented and verified"
run otherca.crt temporal.test
grep -aq "depth=0 CN = client" srv.log && fail "wrong CA: handshake was NOT refused"
echo "ok: wrong CA refused"
run ca.crt wrong.test
grep -aq "depth=0 CN = client" srv.log && fail "wrong server name: handshake was NOT refused"
echo "ok: wrong server name refused"
echo "all handshake checks passed"
