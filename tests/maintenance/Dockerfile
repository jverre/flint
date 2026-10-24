FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM golang:1.26-bookworm AS go
FROM python:3.12-bookworm
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=go /usr/local/go /usr/local/go
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 iptables wireguard-tools iputils-arping iputils-ping curl ca-certificates e2fsprogs util-linux && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md LICENSE /build/
COPY src /build/src
RUN UV_PROJECT_ENVIRONMENT=/opt/test uv sync --project /build --frozen
ENV PATH="/opt/test/bin:/usr/local/go/bin:$PATH" PYTHONPATH=/workspace/src
WORKDIR /workspace
