#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"

CONTEXT="$REPO_ROOT/docker-registry/sidecars/home-mcp"
DOCKERFILE="$CONTEXT/Dockerfile"
IMAGE_BASE="codie-home-mcp"
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
