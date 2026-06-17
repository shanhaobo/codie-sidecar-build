# Shared helpers for docker-registry/scripts/build-*-docker.sh.
# Source this file; do not execute directly.
#
# Inputs (env):
#   REGISTRIES  comma-separated list of registries to push to (default: localhost:5000).
#               Back-compat: if REGISTRIES is unset but REGISTRY is set, REGISTRY is used.
#               Public registries (ghcr.io / Aliyun ACR / Tencent TCR) are pushed over
#               real HTTPS — authenticate with `docker login` (the CI workflow does this)
#               BEFORE invoking the build script. Only plain-HTTP local/compose registries
#               are configured insecure in the builder (see _is_insecure_host).
#   BUILDER     name of the buildx builder (default: agent-builder).
#   PLATFORMS   comma-separated platforms (default: linux/arm64,linux/amd64).

REGISTRIES="${REGISTRIES:-${REGISTRY:-localhost:5000}}"
BUILDER="${BUILDER:-agent-builder}"
PLATFORMS="${PLATFORMS:-linux/arm64,linux/amd64}"
# Comma-separated Docker Hub mirror hosts. When the host's docker daemon
# cannot reach registry-1.docker.io (common on networks where docker.io is
# blocked), set this to one or more mirror hosts so the buildkit container
# can resolve `python:3.11-slim` etc. via the mirror instead.
# Example: DOCKER_HUB_MIRRORS=docker.m.daocloud.io,docker.1panel.live
DOCKER_HUB_MIRRORS="${DOCKER_HUB_MIRRORS:-}"

# Public registries (ghcr.io / Aliyun ACR / Tencent TCR) are real HTTPS and
# validate against system root CAs — no per-registry trust config needed.
# Only plain-HTTP local/compose registries (localhost:5000 and its buildx
# in-container alias) are marked insecure; see _is_insecure_host below.
# (The old self-signed-CA / REGISTRY_TLS path was retired in the 2026-06
# public-registry migration.)

# localhost:5000 → docker-registry-registry-1:5000 (buildx runs in a container,
# so "localhost" is the builder itself, not the host). All other registries
# pass through unchanged.
_push_host() {
    if [[ "$1" == "localhost:5000" ]]; then
        echo "docker-registry-registry-1:5000"
    else
        echo "$1"
    fi
}

# Return 0 if <host> is a plain-HTTP local/compose registry that must be marked
# insecure in buildkit. Public registries (anything with a real domain) are
# HTTPS and must NEVER be marked insecure — buildkit would then try plain HTTP
# and the push/pull fails. Keep this list tight: only loopback + the compose
# alias qualify.
_is_insecure_host() {
    case "$1" in
        localhost|localhost:*|127.0.0.1|127.0.0.1:*|docker-registry-registry-1|docker-registry-registry-1:*)
            return 0 ;;
        *) return 1 ;;
    esac
}

# Split $REGISTRIES into the global array REGISTRY_LIST.
_split_registries() {
    REGISTRY_LIST=()
    local IFS=,
    local reg
    for reg in $REGISTRIES; do
        reg="${reg// /}"
        [ -z "$reg" ] && continue
        REGISTRY_LIST+=("$reg")
    done
}

# compute_push_tags <image-basename> <tag>
# Sets BUILD_TAG_ARGS (array of -t flags for buildx), REGISTRIES_DISPLAY, and
# EXTRA_TAG. When <tag>=latest (the default), each registry gets two parallel
# tags — :latest plus an immutable :YYYYMMDD-HHMM-<short-sha|nogit> — so
# operators can `docker images | grep <name>` to see which build is actually
# running. Bridge keeps resolving :latest; the timestamped tag is for humans.
compute_push_tags() {
    local base="$1"
    local tag="$2"
    _split_registries
    BUILD_TAG_ARGS=()
    REGISTRIES_DISPLAY=""
    EXTRA_TAG=""
    if [[ "$tag" == "latest" ]]; then
        local ts sha
        ts="$(date -u +%Y%m%d-%H%M)"
        if [[ -n "${REPO_ROOT:-}" ]] && command -v git >/dev/null 2>&1; then
            sha="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo nogit)"
        else
            sha="nogit"
        fi
        EXTRA_TAG="${ts}-${sha}"
    fi
    local reg push
    for reg in "${REGISTRY_LIST[@]}"; do
        push="$(_push_host "$reg")"
        BUILD_TAG_ARGS+=(-t "$push/$base:$tag")
        if [[ -n "$EXTRA_TAG" ]]; then
            BUILD_TAG_ARGS+=(-t "$push/$base:$EXTRA_TAG")
        fi
        REGISTRIES_DISPLAY+="$reg "
    done
    if [[ -n "$EXTRA_TAG" ]]; then
        echo "(also tagging :$EXTRA_TAG for traceability)"
    fi
}

# Build the buildx output/destination args into the global OUTPUT_ARGS array.
# Two modes, selected by BUILD_MODE:
#   manifest (default): tag each registry (BUILD_TAG_ARGS) and --push a manifest
#       list directly. Used by local builds and any single-runner multi-arch
#       build. Behavior is byte-identical to the original scripts.
#   digest: push the (single-platform) image BY DIGEST with no tag, and write a
#       --metadata-file so the caller/CI can read containerimage.digest. Used by
#       the native-per-arch CI build jobs; a later `imagetools create` merge step
#       assembles the multi-arch manifest list from each arch's digest. Requires
#       exactly one registry and a single --platform (the merge step fans out to
#       additional registries; cross-arch happens via native runners, not here).
# compute_push_tags must run first (manifest mode reads BUILD_TAG_ARGS).
build_output_args() {
    local base="$1"
    OUTPUT_ARGS=()
    if [[ "${BUILD_MODE:-manifest}" == "digest" ]]; then
        _split_registries
        if [[ "${#REGISTRY_LIST[@]}" -ne 1 ]]; then
            echo "error: BUILD_MODE=digest needs exactly one registry (got: ${REGISTRIES})" >&2
            echo "       multi-registry fan-out is the merge step's job." >&2
            exit 1
        fi
        case "$PLATFORMS" in
            *,*) echo "error: BUILD_MODE=digest needs a single --platform (got: $PLATFORMS)" >&2; exit 1 ;;
        esac
        local push
        push="$(_push_host "${REGISTRY_LIST[0]}")"
        OUTPUT_ARGS+=(--output "type=image,name=$push/$base,push-by-digest=true,name-canonical=true,push=true")
        OUTPUT_ARGS+=(--metadata-file "${METADATA_FILE:-$base-metadata.json}")
    else
        OUTPUT_ARGS+=("${BUILD_TAG_ARGS[@]}" --push)
    fi
}

# Render a buildkitd.toml fragment marking only plain-HTTP local/compose
# registries as insecure HTTP, plus optional docker.io mirrors. Public
# registries get NO entry (default HTTPS + system-root validation). Writes to $1.
_write_buildkitd_config() {
    local out="$1"
    : > "$out"
    _split_registries
    local seen="," reg host
    for reg in "${REGISTRY_LIST[@]}"; do
        for host in "$reg" "$(_push_host "$reg")"; do
            case "$seen" in
                *",$host,"*) continue ;;
            esac
            seen="$seen$host,"
            _is_insecure_host "$host" || continue
            printf '[registry."%s"]\n  http = true\n  insecure = true\n\n' "$host" >> "$out"
        done
    done
    # Optional docker.io mirrors. Without this, machines that cannot reach
    # registry-1.docker.io directly (GFW / VPN-only access) fail at the
    # very first `FROM python:3.11-slim` style line. Mirrors are entered
    # as bare hostnames (or full URLs) and buildkit picks one that works.
    if [ -n "$DOCKER_HUB_MIRRORS" ]; then
        printf '[registry."docker.io"]\n' >> "$out"
        printf '  mirrors = [' >> "$out"
        local IFS=, m first=1
        for m in $DOCKER_HUB_MIRRORS; do
            m="${m// /}"
            [ -z "$m" ] && continue
            if [ $first -eq 1 ]; then first=0; else printf ', ' >> "$out"; fi
            printf '"%s"' "$m" >> "$out"
        done
        printf ']\n\n' >> "$out"
    fi
}

# Return 0 if the running builder already has every required INSECURE registry
# entry in its loaded buildkitd config, 1 otherwise. Public registries need no
# entry, so they are not checked.
_builder_config_matches() {
    local builder_ctr="buildx_buildkit_${BUILDER}0"
    local current
    current="$(docker exec "$builder_ctr" cat /etc/buildkit/buildkitd.toml 2>/dev/null || echo "")"
    _split_registries
    local reg host need_any=0
    for reg in "${REGISTRY_LIST[@]}"; do
        for host in "$reg" "$(_push_host "$reg")"; do
            _is_insecure_host "$host" || continue
            need_any=1
            # buildkit normalizes to single quotes, but accept both forms.
            if ! grep -qE "^\[registry\.['\"]$(printf '%s' "$host" | sed 's/[][\.*^$/]/\\&/g')['\"]\]" <<< "$current"; then
                return 1
            fi
        done
    done
    # No insecure registries to configure (pure public build) → any existing
    # builder is fine.
    return 0
}

# Ensure a buildx builder exists with the right insecure-registry config for
# any plain-HTTP entries in $REGISTRIES. Recreates the builder if config is
# missing or outdated. Connects it to the local registry's docker network so
# the `localhost:5000 → docker-registry-registry-1:5000` alias resolves.
ensure_builder() {
    if docker buildx inspect "$BUILDER" &>/dev/null && _builder_config_matches; then
        :
    else
        if docker buildx inspect "$BUILDER" &>/dev/null; then
            echo "Builder '$BUILDER' missing insecure-registry entries for current REGISTRIES; recreating..."
            docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
        fi
        local cfg
        cfg="$(mktemp -t buildkitd.XXXXXX)"
        _write_buildkitd_config "$cfg"
        echo "Creating buildx builder '$BUILDER' (registries: ${REGISTRIES})..."
        local create_args=(--name "$BUILDER" --driver docker-container --use
                           --buildkitd-config "$cfg")
        # Only attach the local-registry compose network when localhost:5000
        # is actually in REGISTRIES — otherwise the network doesn't exist
        # and buildkit container creation fails with "network not found".
        if [[ " ${REGISTRIES} " == *"localhost:5000"* ]]; then
            create_args+=(--driver-opt network=docker-registry_default)
        fi
        docker buildx create "${create_args[@]}" >/dev/null
        docker buildx inspect --bootstrap "$BUILDER" >/dev/null
        rm -f "$cfg"
    fi

    # Connect builder to local registry's network for the localhost alias.
    if [[ " ${REGISTRIES} " == *"localhost:5000"* ]]; then
        local builder_ctr="buildx_buildkit_${BUILDER}0"
        local reg_net="docker-registry_default"
        if ! docker inspect "$builder_ctr" --format '{{range $k,$_ := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | grep -q "$reg_net"; then
            docker network connect "$reg_net" "$builder_ctr" 2>/dev/null || true
        fi
    fi
}
