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

# Non-root runtime.
RUN useradd -u 10101 -m -d /home/home home
USER home

EXPOSE 8787
ENV HOME_MCP_HOST=0.0.0.0 \
    HOME_MCP_PORT=8787

ENTRYPOINT ["python", "/app/server.py"]
