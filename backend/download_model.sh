#!/bin/sh
# Pull the embedding model into FASTEMBED_CACHE_PATH during the image build.
# Retries transient download failures, then fails the build outright: an image
# without the model would try to download it at runtime as the container user,
# or simply not work in an egress-restricted deployment.
set -u

HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)"
export HF_TOKEN

# Optional CA bundle for networks whose TLS-inspecting proxy re-signs the model
# CDN's certificate. Skipped when the secret is absent or empty (the compose
# default), so the image's own trust store is used.
if [ -s /run/secrets/ca_bundle ]; then
    export SSL_CERT_FILE=/run/secrets/ca_bundle
    export REQUESTS_CA_BUNDLE=/run/secrets/ca_bundle
fi
PYTHON="${PYTHON:-python}"

for attempt in 1 2 3; do
    if "$PYTHON" -c "from fastembed import TextEmbedding; list(TextEmbedding(model_name='BAAI/bge-small-en-v1.5').embed(['warmup']))"; then
        exit 0
    fi
    echo "Model download attempt $attempt failed; retrying in 5s..." >&2
    sleep 5
done

echo "Model download failed after 3 attempts; refusing to build an image without the embedding model" >&2
exit 1
