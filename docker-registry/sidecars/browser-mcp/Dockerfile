# Playwright official image — ships Chromium/Firefox/WebKit + all OS deps
# preinstalled. Tagged to a specific Playwright version for reproducibility.
FROM mcr.microsoft.com/playwright:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Python for the auth proxy layer (Node is already in the base image for
# the Playwright MCP server itself).
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

# Python venv for the proxy
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
RUN pip install --no-cache-dir starlette 'uvicorn[standard]' httpx

# Install Microsoft's official Playwright MCP server. Pin to a specific
# version so the image is reproducible; bump as new tool features land.
# 0.0.75 binary name is `playwright-mcp` (the 0.0.45-era `mcp-server-playwright`
# was renamed upstream).
RUN npm install -g @playwright/mcp@0.0.75

# @playwright/mcp's `--browser` flag does not accept `chromium`, and Chrome
# stable has no Linux/arm64 build, so we install Playwright's bundled
# chromium (multi-arch) and point `--executable-path` at it from the
# entrypoint. Without this step the MCP would try `browser_install` on first
# call and time out under pwuser.
RUN npx -y playwright@1.55.0 install --with-deps chromium \
    && CHROMIUM_BIN="$(find /ms-playwright -maxdepth 4 -type f -path '*chromium*' \( -name headless_shell -o -name chrome \) | head -n1)" \
    && [ -n "$CHROMIUM_BIN" ] && ln -s "$CHROMIUM_BIN" /usr/local/bin/codie-chromium \
    && chown -R pwuser:pwuser /ms-playwright

WORKDIR /app
COPY proxy.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

# Non-root runtime. The Playwright base image already has `pwuser` (uid 1000)
# with the browsers owned/runnable by it.
USER pwuser

EXPOSE 8787
ENV BROWSER_MCP_HOST=0.0.0.0 \
    BROWSER_MCP_PORT=8787 \
    BROWSER_MCP_INTERNAL_PORT=8788

ENTRYPOINT ["/app/entrypoint.sh"]
