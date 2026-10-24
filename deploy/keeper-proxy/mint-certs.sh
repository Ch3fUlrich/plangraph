#!/bin/sh
# Internal CA for the LAN hops behind the OPNsense Caddy edge (D-759 option A; shared by keeper-proxy and
# omnigraph). OPERATOR STEPS (parked): agents never run these on the live CA and never commit the output.
#
#   mint-certs.sh ca     <ca-dir>                           # once: ca.crt + ca.key (key: operator-only, offline)
#   mint-certs.sh server <ca-dir> <out-dir> <dns> [ip]      # per sidecar: server.crt/.key, serverAuth, SAN dns(+ip)
#   mint-certs.sh client <ca-dir> <out-dir> [cn]            # once per edge: client.crt/.key, clientAuth (Caddy)
#
# Conventions: CA "internal hop CA", EC P-256, CA 10 y, leaves 825 d; server SAN = the upstream name Caddy
# dials (tls_server_name) + its LAN IP; Caddy gets ca.crt + client.* at /usr/local/etc/caddy/internal-ca/.
set -eu
cmd=${1:?usage: mint-certs.sh ca|server|client ...}
umask 077
newkey() { openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "$2" \
             -keyout "$1.key" -out "$1.csr" 2>/dev/null; }
signleaf() { # $1=out-prefix $2=ca-dir $3=extfile
  openssl x509 -req -in "$1.csr" -CA "$2/ca.crt" -CAkey "$2/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
    -days 825 -extfile "$3" -out "$1.crt" 2>/dev/null
  rm -f "$1.csr" "$3"; chmod 600 "$1.key"
}
case "$cmd" in
  ca)
    dir=${2:?ca-dir}; mkdir -p "$dir"
    [ -e "$dir/ca.key" ] && { echo "refusing: $dir/ca.key exists" >&2; exit 1; }
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
      -subj "/CN=internal hop CA" -keyout "$dir/ca.key" -out "$dir/ca.crt" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
    chmod 600 "$dir/ca.key"; echo "CA in $dir (keep ca.key offline, operator-only)";;
  server)
    ca=${2:?ca-dir}; out=${3:?out-dir}; dns=${4:?dns name}; ip=${5:-}
    mkdir -p "$out"; san="DNS:$dns"; [ -n "$ip" ] && san="$san,IP:$ip"
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$san" > "$out/server.ext"
    newkey "$out/server" "/CN=$dns"; signleaf "$out/server" "$ca" "$out/server.ext"
    cp "$ca/ca.crt" "$out/ca.crt"; echo "server cert for $san in $out";;
  client)
    ca=${2:?ca-dir}; out=${3:?out-dir}; cn=${4:-caddy-edge}
    mkdir -p "$out"
    printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=clientAuth\n' > "$out/client.ext"
    newkey "$out/client" "/CN=$cn"; signleaf "$out/client" "$ca" "$out/client.ext"
    cp "$ca/ca.crt" "$out/ca.crt"; echo "client cert $cn in $out";;
  *) echo "unknown command $cmd" >&2; exit 2;;
esac
