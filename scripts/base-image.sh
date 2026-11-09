#!/usr/bin/env bash
set -euo pipefail

: "${GHCR_USER:?Set GHCR_USER for GHCR authentication}"
: "${GHCR_TOKEN:?Set GHCR_TOKEN for GHCR authentication}"
out="${BUILD_DIR:-.build}"
mkdir -p "$out"
python scripts/resolve.py 2>&1 | tee "$out/resolve.log"
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
trap 'docker logout ghcr.io' EXIT
args=()
case "${BASE_REBUILD:-false}" in
    true) args+=(--rebuild) ;;
    false) ;;
    *) printf '%s\n' 'BASE_REBUILD must be true or false.' >&2; exit 1 ;;
esac
python scripts/pipeline.py base "${args[@]}"
