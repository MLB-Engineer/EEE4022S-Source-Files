#!/usr/bin/env bash
# Generates a self-signed CA + server certificate for your local_docker
# Mosquitto instance. Not for production/hostinger_tukzie use — that
# broker already has real Let's Encrypt certs via Traefik.
set -euo pipefail

CERT_DIR="$(dirname "$0")/../broker-config/mosquitto/certs"
mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

echo "Generating CA key + cert..."
openssl genrsa -out ca.key 2048
openssl req -new -x509 -days 3650 -key ca.key -out ca.crt \
    -subj "/CN=tukzie-local-dev-ca"

echo "Generating server key + cert signing request..."
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr \
    -subj "/CN=localhost"

echo "Signing server cert with local CA..."
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key \
    -CAcreateserial -out server.crt -days 3650

rm -f server.csr ca.srl

echo "Done. Files in $CERT_DIR:"
ls -la "$CERT_DIR"
echo
echo "Point config.yaml's local_docker.ca_cert_path at: $CERT_DIR/ca.crt"
echo "Point mosquitto.conf at server.crt / server.key (see broker-config/mosquitto/mosquitto.conf)"
