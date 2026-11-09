#!/usr/bin/env bash
set -euo pipefail

: "${GHCR_USER:?Set GHCR_USER for GHCR authentication}"
: "${GHCR_TOKEN:?Set GHCR_TOKEN for GHCR authentication}"
python_bin="${WORKSPACE_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 14))' >/dev/null 2>&1; then
            python_bin="$candidate"
            break
        fi
    done
fi
if [[ -z "$python_bin" ]] ||
   ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 14))' >/dev/null 2>&1; then
    printf '%s\n' 'Python 3.14+ is required; set WORKSPACE_PYTHON if needed.' >&2
    exit 1
fi
out="${BUILD_DIR:-.build}"
mkdir -p "$out"
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
trap 'docker logout ghcr.io' EXIT
case "${BASE_REBUILD:-false}" in
    true)
        BASE_BUILD=true "$python_bin" scripts/resolve.py 2>&1 | tee "$out/resolve.log"
        "$python_bin" scripts/pipeline.py config
        "$python_bin" scripts/pipeline.py base --rebuild
        ;;
    false)
        "$python_bin" scripts/pipeline.py prepare-base
        "$python_bin" scripts/pipeline.py config
        ;;
    *) printf '%s\n' 'BASE_REBUILD must be true or false.' >&2; exit 1 ;;
esac
