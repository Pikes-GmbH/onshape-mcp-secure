#!/bin/sh
# Fly.io mounts volumes owned by root. Fix ownership of /data, then drop to the
# unprivileged "mcp" user (no capabilities, no new privileges) before starting.
set -eu
if [ "$(id -u)" = "0" ]; then
  chown mcp:mcp /data 2>/dev/null || true
  exec setpriv --reuid=mcp --regid=mcp --init-groups --inh-caps=-all --bounding-set=-all --no-new-privs \
    python -m onshape_secure
fi
exec python -m onshape_secure
