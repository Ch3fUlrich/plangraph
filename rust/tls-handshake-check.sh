# Handshake check: throwaway CA + openssl mTLS server (no gRPC), spike driver must present a verified client cert.
set -u
T=$(mktemp -d); cd $T
openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 1 -subj "/CN=test-ca" 2>/dev/null
for n in server client; do
 openssl req -newkey rsa:2048 -nodes -keyout $n.key -out $n.csr -subj "/CN=$n" 2>/dev/null
 printf "subjectAltName=DNS:temporal.test\nextendedKeyUsage=serverAuth,clientAuth\n" > $n.ext
 openssl x509 -req -in $n.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out $n.crt -days 1 -extfile $n.ext 2>/dev/null
done
openssl s_server -accept 17999 -cert server.crt -key server.key -CAfile ca.crt -Verify 1 -alpn h2 -quiet > srv.log 2>&1 &
S=$!; sleep 1
BIN=${CARGO_TARGET_DIR:-target}/debug/driver   # build first: cargo build --bins in rust/spikes/temporal-spike
echo "--- with client cert"
SPIKE_ADDR=https://127.0.0.1:17999 TEMPORAL_TLS_CERT=client.crt TEMPORAL_TLS_KEY=client.key TEMPORAL_TLS_CA=ca.crt TEMPORAL_TLS_SERVER_NAME=temporal.test timeout 20 $BIN 2>&1 | tail -3
sleep 1; echo "--- server saw:"; grep -a -i "verif\|CN\|ALPN" srv.log | head
kill $S 2>/dev/null
openssl s_server -accept 17998 -cert server.crt -key server.key -CAfile ca.crt -Verify 1 -alpn h2 > srv2.log 2>&1 &
S=$!; sleep 1
echo "--- TLS without client cert (plain https, no env)"
SPIKE_ADDR=https://127.0.0.1:17998 timeout 20 $BIN 2>&1 | tail -3
kill $S 2>/dev/null; grep -a -i "verify return\|peer certificate\|error" srv2.log | head -5
rm -rf $T
