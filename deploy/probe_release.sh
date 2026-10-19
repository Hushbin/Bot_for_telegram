#!/bin/bash
set -euo pipefail

# Installed root-owned. Only this fixed command is permitted through sudo.
if (( EUID != 0 || $# != 1 )) || [[ ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Probe rejected: invalid_release' >&2
  exit 2
fi
release=/opt/smebot/releases/$1
if [[ $(readlink -f -- "$release") != "$release" ]] ||
   [[ ! -x $release/.venv/bin/python ]] ||
   [[ ! -f $release/scripts/readiness.py ]] ||
   [[ $(cat "$release/deploy/readiness-protocol" 2>/dev/null) != 1 ]]; then
  echo 'Probe rejected: unsupported_release' >&2
  exit 2
fi

# systemd, rather than a shell, interprets both environment files. The release's
# interpreter and code run only as smebot, never as root. No caller flags or env.
exec /usr/bin/systemd-run --quiet --wait --pipe --collect --service-type=exec \
  -p User=smebot -p Group=smebot -p WorkingDirectory="$release" \
  -p EnvironmentFile=/etc/smebot/bot.env \
  -p EnvironmentFile="$release/deploy/runtime.env" \
  -p RuntimeMaxSec=43 -p TimeoutStopSec=2 -p KillMode=control-group \
  -p NoNewPrivileges=true -p PrivateTmp=true -p ProtectSystem=strict -p ProtectHome=true \
  -- "$release/.venv/bin/python" "$release/scripts/readiness.py"
