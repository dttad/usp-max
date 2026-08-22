# syntax=docker/dockerfile:1.7
#
# Multi-stage build for usp-max.
#
# Stage 1: builds the Python package + Rust extension (`usp_fast`)
# Stage 2: minimal runtime image with only what's needed at runtime
#
# Build:    docker build -t usp-max:1.9.0 .
# Push:     docker push ghcr.io/dttad/usp-max:1.9.0
# Run:      docker run --rm -v $(pwd)/out:/out usp-max:1.9.0 \
#               crawl https://example.com/ -o /out --batch-size 50000 \
#               --compress zstd --tar
#
# Tags baked into the image at build time:
#   USP_MAX_VERSION  : version of the package baked in
#   USP_MAX_GIT_SHA  : git short SHA baked in

# ---------------------------------------------------------------------------
# Stage 1 — builder. Carries cargo, maturin, python-build tooling, the source,
# and produces both the editable install of `ultimate-sitemap-parser` and the
# compiled `usp_fast` PyO3 extension.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ARG DEBIAN_FRONTEND=noninteractive

# Toolchain needed for:
#   maturin (Rust)  -> cargo, rustc
#   Python wheels   -> gcc, libffi-dev, libssl-dev, pkg-config
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cargo \
        curl \
        gcc \
        git \
        libffi-dev \
        libssl-dev \
        pkg-config \
        rustc \
    && rm -rf /var/lib/apt/lists/*

# Install uv (single binary, statically linked)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv \
    && uv --version

ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /build

# Bring in the source first so Docker can cache the dependency layer
COPY pyproject.toml uv.lock README.md README.rst LICENSE NOTICE ./
COPY usp ./usp
COPY rust ./rust
COPY bench ./bench

# Build the package + Rust extension into an isolated prefix.
# We use a venv-free install so the prefix is a normal site-packages dir.
ENV VIRTUAL_ENV=/install
ENV PATH="/install/bin:${PATH}"
RUN uv venv /install
RUN uv pip install --python /install/bin/python \
        '.[fast]' \
        pytest requests-mock pytest-mock vcrpy \
    && cd /build/rust \
    && PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 \
       /install/bin/maturin build --release \
            --out /install/wheels \
    && uv pip install --python /install/bin/python --no-deps \
        /install/wheels/usp_fast-*.whl \
    && /install/bin/python -c "import usp, usp_fast; print('usp', usp.__version__, 'usp_fast', usp_fast)"


# ---------------------------------------------------------------------------
# Stage 2 — runtime. Slim Python image with just the install prefix and
# the compiled extension copied over.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG DEBIAN_FRONTEND=noninteractive

# Only the libraries that httpx[http2] needs at runtime:
#   h2 over SSL.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libssl3 \
    && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt install prefix from stage 1.
COPY --from=builder /install /install

# Best practice: run as a non-root user. Give uspmax ownership of
# /install so its pip-installed packages are usable, and create the
# default output dir owned by uspmax.
RUN useradd --create-home --uid 1000 uspmax \
    && chown -R uspmax:uspmax /install

ENV PATH="/install/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

USER uspmax
WORKDIR /home/uspmax
RUN mkdir -p /home/uspmax/out
VOLUME ["/home/uspmax/out"]
WORKDIR /home/uspmax/out

# The CLI entrypoint. The `crawl` subcommand is required; everything else
# (URL, --batch-size, --compress, etc.) is forwarded to `usp-max crawl`.
ENTRYPOINT ["python", "-m", "usp.cli_main"]
CMD ["crawl", "--help"]

# Image labels (OCI standard)
LABEL org.opencontainers.image.title="usp-max" \
      org.opencontainers.image.description="High-performance sitemap crawler (fork of ultimate-sitemap-parser)" \
      org.opencontainers.image.source="https://github.com/dttad/usp-max" \
      org.opencontainers.image.licenses="GPL-3.0-or-later" \
      org.opencontainers.image.vendor="dttad"