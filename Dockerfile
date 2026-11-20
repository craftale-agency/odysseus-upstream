# Both stages need the fixed native decoder, including curl in the builder and
# distro Node in the runtime. A cached Python base may predate the Debian fix.
FROM python:3.14.9-slim AS brotli-base
COPY scripts/check_brotli.py /usr/local/bin/check-brotli.py
COPY scripts/check_ca_trust.py /usr/local/bin/check-ca-trust.py
# Check inherited trust before the first HTTPS package request, retaining Debian's
# signed archive verification. Refresh only the explicit native dependency.
RUN python /usr/local/bin/check-ca-trust.py --system \
    && sed -i 's|http://deb.debian.org/|https://deb.debian.org/|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends libbrotli1 \
    && python /usr/local/bin/check-brotli.py \
    && rm -rf /var/lib/apt/lists/*

# ---- builder: patch + build wheels for Real-ESRGAN's broken-on-3.14 deps ----
# basicsr/gfpgan/facexlib read their version via exec()+locals()['__version__'],
# which raises KeyError on Python 3.13+ (PEP 667). Build patched wheels here so
# the final image / Cookbook never has to compile the broken sdists. See
# docker/build-realesrgan-wheels.sh for the full rationale.
FROM brotli-base AS realesrgan-wheels
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/check_ca_trust.py /usr/local/bin/check-ca-trust.py
RUN python /usr/local/bin/check-ca-trust.py --system
COPY requirements-bootstrap.txt /tmp/requirements-bootstrap.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements-bootstrap.txt \
    && python /usr/local/bin/check-ca-trust.py --python
COPY docker/build-realesrgan-wheels.sh /usr/local/bin/build-realesrgan-wheels.sh
RUN bash /usr/local/bin/build-realesrgan-wheels.sh /wheels

FROM brotli-base

# System deps. tmux is required by Cookbook for background downloads/serves.
# openssh-client is required for Cookbook remote server tests, setup, probes,
# downloads, and serves from Docker installs.
# git/cmake are required when Cookbook builds llama.cpp on first llama.cpp
# launch inside Docker.
# nodejs/npm provide npx for the built-in Browser MCP server.
# chromium provides the actual browser binary used by that MCP server.
# fontconfig + Noto CJK provide real fallback glyphs for multilingual pages;
# Chromium otherwise renders Chinese/Japanese/Korean labels as empty boxes.
# iproute2/iputils-ping/net-tools/dnsutils/nmap give Docker-hosted agents the
# basic network inspection toolkit expected by local LAN/debugging tasks.
# gosu lets the entrypoint drop privileges cleanly so signals still reach
# uvicorn directly (no extra shell layer like `su`/`sudo` would add).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    curl \
    ca-certificates \
    git \
    nodejs \
    npm \
    ripgrep \
    chromium \
    fontconfig \
    fonts-noto-cjk \
    tmux \
    openssh-client \
    iproute2 \
    iputils-ping \
    net-tools \
    dnsutils \
    nmap \
    gosu \
    libgl1 \
    libglib2.0-0t64 \
    libxcb1 \
    libmagic1 \
    && python /usr/local/bin/check-brotli.py --node /usr/bin/node \
    && rm -rf /var/lib/apt/lists/*

# Refresh both Python trust bundles before installing application dependencies.
# System roots also serve web fetches, webhooks, curl, and Node's OpenSSL mode.
COPY scripts/check_ca_trust.py /usr/local/bin/check-ca-trust.py
RUN python /usr/local/bin/check-ca-trust.py --system
COPY requirements-bootstrap.txt /tmp/requirements-bootstrap.txt
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r /tmp/requirements-bootstrap.txt \
    && python /usr/local/bin/check-ca-trust.py --python
ENV NODE_OPTIONS=--use-openssl-ca \
    GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt

# Chromium has a separate root store. Distrust the compromised intermediate
# public keys explicitly, including cross-signs, without waiting for CRL updates.
COPY docker/chromium-emudhra.json /etc/chromium/policies/managed/odysseus-emudhra.json

# Private browser automation wrapper used by the native `private_browser` tool.
# Chromium is installed above, so agent-browser can drive the existing browser
# binary without paying `npx` startup/install overhead on each tool call.
RUN npm install -g agent-browser@0.35.0 --omit=dev --loglevel=error

# libgl1/libglib2.0-0t64/libxcb1 are runtime shared libs (libGL.so.1,
# libglib-2.0/libgthread, libxcb.so.1) that opencv-python (cv2) loads. The
# slim base omits them, so the Cookbook "install realesrgan" path imports cv2
# and dies with `libxcb.so.1: cannot open shared object file` despite a clean
# pip install. Using full opencv-python (not -headless) because basicsr/gfpgan/
# facexlib/realesrgan all depend on the `opencv-python` distribution by name.
#
# libmagic1 is the shared lib (libmagic.so.1) that python-magic dlopens for
# content-based MIME sniffing in src/upload_handler.py. We install both here
# (libmagic1 + the python-magic wrapper, below) rather than in requirements.txt
# because python-magic resolves libmagic at import time: where the lib is
# absent the import can block or raise, so keeping it image-only avoids
# regressing pip/venv installs on hosts without libmagic. Debian always has the
# lib here, so the import is instant and detection actually works.

# Docker CLI (client only — daemon stays on the host via the
# /var/run/docker.sock mount). The Debian `docker.io` package ships
# dockerd but not the client binary on slim, so grab the static client
# tarball from download.docker.com instead.
# 29.9.2 includes Go 1.26.10's eMudhra intermediate-key distrust.
ARG DOCKER_CLI_VERSION=29.9.2
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) DARCH=x86_64 ;; \
         arm64) DARCH=aarch64 ;; \
         *) echo "unsupported arch $ARCH"; exit 1 ;; \
       esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DARCH}/docker-${DOCKER_CLI_VERSION}.tgz" \
       -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && install -m 0755 /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

# GitHub CLI — read-mostly GitHub access for the agent (issues/PRs are NOT in
# the github-readonly MCP preset). Pinned tarball install; auth is NOT baked in:
# the token lives at /app/data/gh-token.txt (volume) and callers wrap it as
# GH_TOKEN=$(cat /app/data/gh-token.txt) gh ... — see the github-lookup skill.
ARG GH_CLI_VERSION=2.99.0
RUN curl -fsSL https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_amd64.tar.gz \
    -o /tmp/gh.tgz \
    && tar -xzf /tmp/gh.tgz -C /tmp \
    && install -m 0755 /tmp/gh_${GH_CLI_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh \
    && rm -rf /tmp/gh*

WORKDIR /app

# Install Python deps first (layer cache). Optional extras (PyMuPDF AGPL, etc.)
# are opt-in so the default image stays MIT-core; see requirements-optional.txt.
ARG INSTALL_OPTIONAL=false
COPY requirements.txt requirements-optional.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_OPTIONAL" = "true" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi

# python-magic powers content-based MIME sniffing in src/upload_handler.py.
# Image-only (not in requirements.txt) because it needs the libmagic1 system
# lib installed above; see the apt note near the top of this stage.
RUN pip install --no-cache-dir python-magic==0.4.27

# Pre-install the patched basicsr/gfpgan/facexlib wheels built in the
# realesrgan-wheels stage (--no-deps keeps the image lean — torch & friends are
# pulled only when realesrgan is actually installed). With these dists already
# satisfied, the Cookbook's plain `pip install realesrgan` resolves them from
# wheels instead of rebuilding the sdists that fail on Python 3.14.
COPY --from=realesrgan-wheels /wheels/ /tmp/odysseus-wheels/
RUN pip install --no-cache-dir --no-deps /tmp/odysseus-wheels/*.whl \
    && rm -rf /tmp/odysseus-wheels

# basicsr.data.degradations imports torchvision.transforms.functional_tensor,
# which torchvision removed in 0.17+. In-app paths shim it at runtime
# (src/optional_deps.py, wired via routes/shell_routes.py and
# routes/gallery/gallery_routes.py); patch the installed module too so
# Cookbook/agent scripts importing realesrgan in a bare interpreter work
# without knowing about the shim.
RUN sed -i 's/from torchvision.transforms.functional_tensor import rgb_to_grayscale/from torchvision.transforms.functional import rgb_to_grayscale/' \
        /usr/local/lib/python3.14/site-packages/basicsr/data/degradations.py

# ---- CUDA 13 runtime libs + onnxruntime-gpu (GPU embeddings) -------------
# fastembed depends on the CPU-only `onnxruntime` wheel. On Python 3.14 the
# onnxruntime-gpu builds published on PyPI (>= 1.24) are CUDA 13 builds —
# libonnxruntime_providers_cuda.so links libcublasLt.so.13 / libcublas.so.13
# / libcurand.so.10 / libcudart.so.13 (verified via readelf) — and the
# Microsoft CUDA-12 index only publishes cp311-cp313 plus cp314t wheels, so
# there is NO CUDA-12 onnxruntime-gpu for this image. The image therefore
# ships the CUDA 13 runtime libraries itself; the driver side (libcuda.so.1)
# is injected at runtime by the NVIDIA Container Toolkit (see
# docker-compose.gpu-nvidia.yml, NVIDIA_DRIVER_CAPABILITIES=compute,utility).
# Without these libs the CUDA EP fails to load ("libcublasLt.so.13: cannot
# open shared object file") and fastembed silently falls back to
# CPUExecutionProvider (live breakage on crafthost, first seen 2026-09-16).
# CUDA 13.1 packages: the lowest 13.x driver requirement (>= 580) while still
# satisfying the .so.13 SONAMEs; cuDNN 9 because some models request it at
# session init even though ort does not hard-link it. Base is Debian 13
# (python:3.14-slim = trixie), hence the debian13 repo.
ARG CUDA_KEYRING_URL=https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/cuda-keyring_1.1-1_all.deb
RUN curl -fsSL -o /tmp/cuda-keyring.deb "${CUDA_KEYRING_URL}" \
    && dpkg -i /tmp/cuda-keyring.deb \
    && rm -f /tmp/cuda-keyring.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        cuda-cudart-13-1 \
        libcublas-13-1 \
        libcurand-13-1 \
        libcudnn9-cuda-13 \
    && rm -rf /var/lib/apt/lists/*

# Swap fastembed's CPU onnxruntime for the CUDA build of the SAME version so
# embeddings run on the GPU when the runtime exposes one. Same-version swap
# avoids API drift with fastembed; on hosts without a GPU the CUDA EP is
# simply unavailable and ORT falls back to CPU — behavior unchanged.
ARG ORT_GPU_VERSION=1.30.0
RUN pip uninstall -y onnxruntime \
    && pip install --no-cache-dir "onnxruntime-gpu==${ORT_GPU_VERSION}"

# Copy app code
COPY . .
# Require the redistribution notices in the image build context.
COPY licenses/ ./licenses/
COPY THIRD_PARTY_PROVENANCE.json ACKNOWLEDGMENTS.md ./

# Create data directory (mount a volume here for persistence)
RUN mkdir -p data logs services/cache/search

# Entrypoint that drops to PUID/PGID (default 1000:1000) and repairs
# ownership on the bind-mounted /app/data and /app/logs. Without this,
# the container runs as root and writes root-owned files into host
# bind mounts — any later non-root run (or a host user trying to
# update them) silently fails on EPERM, breaking skill extraction,
# prefs persistence, mail attachments, etc.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 7000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7000"]
