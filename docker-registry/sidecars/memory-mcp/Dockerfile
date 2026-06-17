FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY server.py ./

# Non-root runtime. /data is where the named volume codie-memory-data
# mounts; Docker copies this dir's ownership onto NEW named volumes, so
# the chown is what makes the sqlite file writable at runtime.
RUN useradd -u 10101 -m -d /home/memory memory && \
    mkdir -p /data && chown memory:memory /data
USER memory

VOLUME /data

EXPOSE 8787
ENV MEMORY_MCP_HOST=0.0.0.0 \
    MEMORY_MCP_PORT=8787 \
    MEMORY_DB=/data/memory.db

ENTRYPOINT ["python", "/app/server.py"]
