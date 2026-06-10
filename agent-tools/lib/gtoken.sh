#!/usr/bin/env bash
# Mint a Google OAuth access token from a service-account key file.
# Usage: gtoken.sh <key-file.json> <scope>
# Uses an isolated CLOUDSDK_CONFIG so it never touches the user's gcloud login.
set -euo pipefail

KEY_FILE="$1"
SCOPE="$2"

[ -f "$KEY_FILE" ] || { echo "gtoken: key file not found: $KEY_FILE" >&2; exit 1; }

export CLOUDSDK_CONFIG="$(mktemp -d /tmp/gtoken.XXXXXX)"
trap 'rm -rf "$CLOUDSDK_CONFIG"' EXIT

gcloud auth activate-service-account --key-file="$KEY_FILE" --quiet >/dev/null 2>&1
gcloud auth print-access-token --scopes="$SCOPE" 2>/dev/null
