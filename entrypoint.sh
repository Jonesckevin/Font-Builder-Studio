#!/bin/sh
set -eu

APP_USER="${APP_USER:-appuser}"

# Auto-generate signing secrets if the operator did not supply them. Set these
# explicitly (see docker-compose.yml) to keep sessions/tokens valid across
# restarts.
if [ -z "${SECRET_KEY:-}" ]; then
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  export SECRET_KEY
  echo "Generated ephemeral SECRET_KEY"
fi
if [ -z "${SECRETS:-}" ]; then
  SECRETS="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  export SECRETS
  echo "Generated ephemeral SECRETS"
fi

# The image runs as the unprivileged `appuser` by default (see USER in the
# Dockerfile), so no privilege drop is needed here. Dropping at runtime would
# require CAP_SETUID, which compose removes via `cap_drop: [ALL]`.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /data/output /data/uploaded /data/projects 2>/dev/null || true
  chown -R "$APP_USER":"$APP_USER" /data 2>/dev/null || true
  echo "WARNING: running as root. Set the container user to '$APP_USER' (uid 1000) for least privilege." >&2
fi

# /data is often a host bind mount. An earlier run (or a host directory owned
# by another uid) can leave a subdirectory that this unprivileged user cannot
# write to, which would silently break persistence. Recreate those, since an
# empty directory can be removed by anyone with write access to its parent.
for name in output uploaded projects; do
  target="/data/$name"
  if [ -d "$target" ] && [ ! -w "$target" ]; then
    if rmdir "$target" 2>/dev/null && mkdir -p "$target" 2>/dev/null; then
      echo "Recreated non-writable $target" >&2
    else
      echo "WARNING: $target is not writable and could not be recreated." >&2
    fi
  fi
done

if ! mkdir -p /data/output /data/uploaded /data/projects 2>/dev/null; then
  echo "WARNING: /data is not writable - persistence is unavailable." >&2
fi

WORKERS="${GUNICORN_WORKERS:-3}"
# NOTE: --threads MUST stay at 1. FontForge keeps process-global state and is
# not thread-safe; all font work is additionally isolated in a subprocess.
echo "Starting Font Builder Studio (gunicorn: ${WORKERS} workers x 1 thread)"
exec gunicorn \
  --bind 0.0.0.0:5000 \
  --workers "$WORKERS" \
  --threads 1 \
  --timeout 180 \
  --access-logfile - \
  --error-logfile - \
  app:app
