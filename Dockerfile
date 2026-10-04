# syntax=docker/dockerfile:1
#
# Font Builder Studio
# ===================
# Multi-stage build on Ubuntu 24.04 (LTS, Python 3.12) because the font engine
# is FontForge's Python bindings installed from the distro:
#
#   python3-fontforge  ->  provides the `fontforge` and `psMat` modules
#
# Those modules live in the *system* dist-packages
# (/usr/lib/python3/dist-packages), so the virtualenv is created with
# --system-site-packages. A plain venv would not see them.
#
# Stages:
#   base     - system packages, venv, Python dependencies, the appuser account
#   indexer  - generates the Unicode index, which needs tools/ that the image
#              must not ship. Only the resulting data file is copied onwards.
#   runtime  - the production image (default target). No tests/, no tools/.
#   test     - runtime plus tests/ and tools/, for running the suites.
#
# FontForge is GPL-3.0-or-later; see README.md for the licensing note.

ARG UBUNTU_BASE=ubuntu:24.04
FROM ${UBUNTU_BASE} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# --- System packages -------------------------------------------------------
# universe is required for python3-fontforge. The official ubuntu image enables
# it by default, but we assert it defensively before installing.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \
      sed -i 's/^Components: main$/Components: main universe/' \
        /etc/apt/sources.list.d/ubuntu.sources || true; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      python3 \
      python3-venv \
      python3-pip \
      python3-fontforge \
      libmagic1 \
      util-linux \
      ca-certificates \
    ; \
    rm -rf /var/lib/apt/lists/*

# --- Python environment ----------------------------------------------------
# --system-site-packages is REQUIRED so the venv can import the apt-installed
# fontforge / psMat modules.
RUN python3 -m venv --system-site-packages /opt/venv \
 && pip install --upgrade pip setuptools wheel

WORKDIR /app

# The application lives in App/; only the Docker files sit at the repo root.
COPY App/requirements.txt App/requirements-auth.txt ./
RUN pip install -r requirements.txt \
 && pip install -r requirements-auth.txt

# --- Unprivileged runtime --------------------------------------------------
# Ubuntu 24.04 already ships a user/group at uid/gid 1000 (`ubuntu`), so it is
# removed first to keep the app's uid/gid deterministically 1000 (which makes
# bind-mounted volume permissions predictable).
RUN set -eux; \
    userdel -r ubuntu 2>/dev/null || true; \
    groupdel ubuntu 2>/dev/null || true; \
    groupadd --gid 1000 appuser; \
    useradd --uid 1000 --gid 1000 --create-home --home-dir /home/appuser appuser; \
    mkdir -p /data/output /data/uploaded /data/projects; \
    chown -R appuser:appuser /data /home/appuser

# Defaults shared by both shipping stages.
#
# PYTHONUNBUFFERED pushes log lines straight to `docker logs` instead of letting
# them sit in a buffer. Baked in because it matters however the image is run,
# not only under compose.
ENV HOME=/data \
    PROJECTS_DIR=/data/projects \
    UPLOAD_DIR=/data/uploaded \
    OUTPUT_DIR=/data/output \
    MAX_UPLOAD_MB=25 \
    ALLOW_AUTH=false \
    REQUIRE_AUTH=false \
    ALLOW_USER_REGISTRATION=true \
    ALLOW_GUEST_LOGIN=true \
    REQUIRE_SECRETS=false \
    TRUST_PROXY=false \
    GUNICORN_WORKERS=3 \
    PYTHONUNBUFFERED=1

# ============================================================================
# indexer - builds the character-picker index
# ============================================================================
# Separate stage on purpose: generating the index needs tools/, and tools/ must
# not exist in the production image. Only the finished data file is copied out.
FROM base AS indexer
COPY App/ ./
RUN python3 tools/build_unicode_index.py

# ============================================================================
# test - everything the production image has, plus tests/ and tools/
# ============================================================================
# Declared BEFORE runtime so that runtime stays the final (default) stage: a
# bare `docker build .` must never produce a test-riddled image. Used by the
# `web-test` service in docker-compose.test.yml.
FROM base AS test
COPY entrypoint.sh ./
COPY App/ ./
RUN python3 tools/build_unicode_index.py
RUN set -eux; \
    sed -i 's/\r$//' /app/entrypoint.sh; \
    chmod +x /app/entrypoint.sh; \
    chown -R appuser:appuser /app

EXPOSE 5000
USER appuser
CMD ["/app/entrypoint.sh"]

# ============================================================================
# runtime - the production image (final stage, therefore the default target)
# ============================================================================
# tests/ and tools/ are never copied in, so they cannot reach a running
# container even by accident - there is no path to remove, rather than a layer
# that deletes them after the fact.
FROM base AS runtime
COPY entrypoint.sh ./
COPY App/*.py App/config.json ./
COPY App/static ./static
COPY App/templates ./templates
COPY --from=indexer /app/resources ./resources

# Drop the build tooling. pip, setuptools and wheel exist to install packages;
# nothing here runs them. Between them they vendor urllib3, msgpack, requests,
# wheel and zipp, which accounted for every non-OS finding the scanner reported
# (0 CRITICAL, 4 HIGH) - none of which the application can reach, since the
# vendored copies are private to pip and setuptools. Removing them takes the
# build tooling out of production rather than accepting it as unreachable.
# The test stage keeps pip, which is one reason it is a separate image.
RUN set -eux; \
    pip uninstall -y setuptools wheel pip; \
    rm -rf /opt/venv/lib/python3.12/site-packages/_distutils_hack \
           /opt/venv/lib/python3.12/site-packages/distutils-precedence.pth \
           /opt/venv/lib/python3.12/site-packages/pkg_resources \
           /opt/venv/lib/python3.12/site-packages/pip \
           /opt/venv/lib/python3.12/site-packages/pip-*.dist-info \
           /opt/venv/lib/python3.12/site-packages/setuptools \
           /opt/venv/lib/python3.12/site-packages/setuptools-*.dist-info \
           /opt/venv/lib/python3.12/site-packages/wheel \
           /opt/venv/lib/python3.12/site-packages/wheel-*.dist-info; \
    find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} +

RUN set -eux; \
    sed -i 's/\r$//' /app/entrypoint.sh; \
    chmod +x /app/entrypoint.sh; \
    chown -R appuser:appuser /app

EXPOSE 5000

# Run unprivileged by default. The entrypoint does NOT need root: dropping
# privileges at runtime would require CAP_SETUID, which compose removes via
# `cap_drop: [ALL]`.
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=3).status==200 else 1)" || exit 1

CMD ["/app/entrypoint.sh"]
