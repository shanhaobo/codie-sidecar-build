FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Media/document toolchain. yt-dlp is installed via pip (far more up-to-date
# than Debian's package) — keep it here so the Python venv owns all three.
# Size-sensitive: no recommends, no docs, clean apt cache.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg imagemagick poppler-utils pandoc \
        ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# ImageMagick 6 ships a restrictive policy.xml that blocks PDF/PS/MVG by
# default; since this runs in an isolated container reachable only by the
# paired agent, relax the policy so the agent can convert PDFs.
RUN if [ -f /etc/ImageMagick-6/policy.xml ]; then \
        sed -i 's|<policy domain="coder" rights="none" pattern="\(PDF\|PS\|PS2\|PS3\|EPS\|XPS\|MVG\|LABEL\)"|<policy domain="coder" rights="read\|write" pattern="\1"|g' /etc/ImageMagick-6/policy.xml; \
    fi

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY server.py ./

# yt-dlp via pip (more current than Debian's; drags in minimal deps).
RUN pip install --no-cache-dir 'yt-dlp>=2025.0.0'

# Non-root runtime.
RUN useradd -u 10100 -m -d /home/media media && \
    mkdir -p /workspace && chown media:media /workspace
USER media

EXPOSE 8787
ENV MEDIA_MCP_HOST=0.0.0.0 \
    MEDIA_MCP_PORT=8787 \
    MEDIA_MCP_WORKSPACE=/workspace

ENTRYPOINT ["python", "/app/server.py"]
