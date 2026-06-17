#!/usr/bin/env bash
# Start the Playwright MCP server on an internal (127.0.0.1-only) port,
# then launch the Python auth proxy on the external port. The proxy
# verifies Bearer tokens before forwarding to the internal server.
#
# Lifecycle: when the proxy (PID 1 via exec) exits, Docker stops the
# container; a bg-trap kills the playwright process so no orphan remains.
set -euo pipefail

: "${BROWSER_MCP_TOKEN:?BROWSER_MCP_TOKEN env var required}"
: "${BROWSER_MCP_INTERNAL_PORT:=8788}"
: "${BROWSER_MCP_PORT:=8787}"
: "${BROWSER_MCP_HOST:=0.0.0.0}"

# Headless by default; set BROWSER_MCP_HEADED=1 to show the window (only
# useful when X11/VNC is wired in — future Phase 5 work).
# --no-sandbox: docker containers don't have unprivileged-user-namespaces by
# default, so chromium's setuid sandbox fails ("Chromium sandboxing failed!").
# We trade the in-process sandbox for the container boundary itself.
PW_ARGS=(--host 127.0.0.1 --port "$BROWSER_MCP_INTERNAL_PORT" --no-sandbox)
if [[ "${BROWSER_MCP_HEADED:-}" != "1" ]]; then
    PW_ARGS+=(--headless)
fi

# Chrome stable is unavailable on Linux/arm64; we ship Playwright's bundled
# chromium and tell MCP to use it directly via --executable-path. The symlink
# is created at build time (see Dockerfile).
if [[ -x /usr/local/bin/codie-chromium ]]; then
    PW_ARGS+=(--browser chrome --executable-path /usr/local/bin/codie-chromium)
fi

echo "[browser_mcp] starting @playwright/mcp on 127.0.0.1:$BROWSER_MCP_INTERNAL_PORT (headless=${BROWSER_MCP_HEADED:-0})" >&2
playwright-mcp "${PW_ARGS[@]}" &
PLAYWRIGHT_PID=$!

cleanup() {
    # Forward SIGTERM to the playwright child so Docker stop is clean.
    if kill -0 "$PLAYWRIGHT_PID" 2>/dev/null; then
        kill -TERM "$PLAYWRIGHT_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "[browser_mcp] starting auth proxy on $BROWSER_MCP_HOST:$BROWSER_MCP_PORT" >&2
exec python3 /app/proxy.py
