#!/bin/sh
# YC searches /function/code before its own pip. Keep the provider's arguments.
set -eu
printf '%s\n' 'AUTOREPEATER_PIP_TLS: ENTER'
AR_CA_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PIP_CERT="$AR_CA_ROOT/cloud-ca/ca-bundle.pem"
export REQUESTS_CA_BUNDLE="$PIP_CERT"
export SSL_CERT_FILE="$PIP_CERT"
python3.12 - "$AR_CA_ROOT" <<'PY'
from hashlib import sha256
import json
from pathlib import Path
import ssl
import sys
root = Path(sys.argv[1])
bundle = root / 'cloud-ca/ca-bundle.pem'
expected = json.loads((root / 'cloud-ca/manifest.json').read_text())['bundle_sha256']
if sha256(bundle.read_bytes()).hexdigest() != expected:
    raise SystemExit('cloud CA bundle hash mismatch')
context = ssl.create_default_context(cafile=str(bundle))
print('AUTOREPEATER_PIP_TLS: BUNDLE_OK', flush=True)
PY
printf '%s\n' 'AUTOREPEATER_PIP_TLS: EXEC_PIP'
exec python3.12 -m pip --cert "$PIP_CERT" "$@"
