#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )) || [[ ! $1 =~ ^[0-9a-f]{40}$ ]] || [[ ! $2 =~ ^[0-9a-f]{64}$ ]]; then
  echo 'Usage: smebot-activate <commit-sha> <archive-sha256>' >&2
  exit 2
fi

revision=$1
archive=/opt/smebot/incoming/${revision}.tar.gz
release=/opt/smebot/releases/${revision}
[[ $(id -un) == smebot-deploy ]]
# GitHub serializes workflow jobs; this also serializes local/manual activations.
exec 9>/opt/smebot/.activation.lock
flock -n 9 || { echo 'Activation rejected: activation_in_progress' >&2; exit 1; }
[[ -f $archive ]]
printf '%s  %s\n' "$2" "$archive" | sha256sum -c -
if [[ ! -x $release/.venv/bin/python ]]; then
  rm -rf "$release"
  mkdir -m 0755 "$release"
  trap 'rm -rf "$release"' ERR
  tar -xzf "$archive" -C "$release" --no-same-owner --no-same-permissions
  python3 -m venv "$release/.venv"
  "$release/.venv/bin/python" -m pip install --no-index --find-links "$release/wheelhouse" -r "$release/requirements.txt"
  rm -rf "$release/wheelhouse"
  "$release/.venv/bin/python" -m compileall -q "$release"
  trap - ERR
fi

# The controller is installed root-owned independently of the candidate archive.
exec /usr/bin/python3 /usr/local/lib/smebot/activation.py "$revision"
