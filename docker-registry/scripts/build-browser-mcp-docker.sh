#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# Default to both platforms so images can be distributed to x86 + ARM
# machines. Playwright's upstream base is ~800MB per arch; if you're
# iterating locally and only need your own arch, invoke with e.g.
# `PLATFORMS=linux/arm64 bash scripts/build-browser-mcp-docker.sh`.
# (PLATFORMS env is consumed by _lib.sh — set it before sourcing.)
PLATFORMS="${PLATFORMS:-linux/arm64,linux/amd64}"

# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"

CONTEXT="$REPO_ROOT/docker-registry/sidecars/browser-mcp"
DOCKERFILE="$CONTEXT/Dockerfile"
IMAGE_BASE="codie-browser-mcp"
TAG="${1:-latest}"

if [ ! -f "$DOCKERFILE" ]; then
    echo "error: Dockerfile not found at $DOCKERFILE"
    exit 1
fi

ensure_builder
compute_push_tags "$IMAGE_BASE" "$TAG"
build_output_args "$IMAGE_BASE"

echo "Building $IMAGE_BASE:$TAG for $PLATFORMS → ${REGISTRIES_DISPLAY}..."
docker buildx build --builder "$BUILDER" \
    --platform "$PLATFORMS" \
    -f "$DOCKERFILE" \
    "${OUTPUT_ARGS[@]}" \
    "$CONTEXT"

echo ""
echo "Done. Image: $IMAGE_BASE:$TAG ($PLATFORMS) → ${REGISTRIES_DISPLAY}"
