#!/usr/bin/env bash
# Fetch the Piper voice and the openWakeWord ONNX models.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p models/piper models/whisper

VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
if [ ! -f models/piper/en_US-lessac-medium.onnx ]; then
    curl -L --fail --retry 2 -o models/piper/en_US-lessac-medium.onnx \
        "$VOICE_BASE/en_US-lessac-medium.onnx"
    curl -L --fail --retry 2 -o models/piper/en_US-lessac-medium.onnx.json \
        "$VOICE_BASE/en_US-lessac-medium.onnx.json"
fi

.venv/bin/python - <<'PY'
import openwakeword.utils as utils
utils.download_models()
PY

echo "models ready"
