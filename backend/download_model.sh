#!/bin/sh
# Pull the embedding model into FASTEMBED_CACHE_PATH during the image build.
# Retries transient download failures, then fails the build outright: an image
# without the model would try to download it at runtime as the container user,
# or simply not work in an egress-restricted deployment.
set -u

HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)"
export HF_TOKEN

PYTHON="${PYTHON:-python}"

# Optional CA bundle for networks whose TLS-inspecting proxy re-signs the model
# CDN's certificate. Skipped when the secret is absent or empty (the compose
# default), so the image's own trust store is used. SSL_CERT_FILE and
# REQUESTS_CA_BUNDLE replace the default trust store rather than extend it, so
# the secret's CAs are appended to the system bundle (or certifi's) in a
# combined file; a secret holding only the proxy root then still lets hosts the
# proxy does not intercept verify.
if [ -s /run/secrets/ca_bundle ]; then
    CA_SYSTEM=/etc/ssl/certs/ca-certificates.crt
    if [ ! -s "$CA_SYSTEM" ]; then
        CA_SYSTEM="$("$PYTHON" -c "import certifi; print(certifi.where())" 2>/dev/null || true)"
    fi
    CA_COMBINED="$(mktemp)"
    # Remove the combined bundle on any exit; an EXIT trap keeps the exit status.
    trap 'rm -f "$CA_COMBINED"' EXIT
    if [ -n "$CA_SYSTEM" ] && [ -s "$CA_SYSTEM" ]; then
        cat "$CA_SYSTEM" > "$CA_COMBINED"
        # Guard against a system bundle that lacks a trailing newline.
        echo >> "$CA_COMBINED"
    else
        echo "Warning: no system CA bundle found; trusting only the ca_bundle secret" >&2
        : > "$CA_COMBINED"
    fi
    cat /run/secrets/ca_bundle >> "$CA_COMBINED"
    export SSL_CERT_FILE="$CA_COMBINED"
    export REQUESTS_CA_BUNDLE="$CA_COMBINED"
fi

for attempt in 1 2 3; do
    if "$PYTHON" -c "from fastembed import TextEmbedding; list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5').embed(['warmup']))"; then
        exit 0
    fi
    echo "Model download attempt $attempt failed; retrying in 5s..." >&2
    sleep 5
done

echo "Model download failed after 3 attempts; refusing to build an image without the embedding model" >&2
exit 1
