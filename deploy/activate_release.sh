#!/usr/bin/env bash
set -euo pipefail

if (( $# != 2 )) || [[ ! $1 =~ ^[0-9a-f]{40}$ ]] || [[ ! $2 =~ ^[0-9a-f]{64}$ ]]; then
  echo 'Usage: smebot-activate <commit-sha> <archive-sha256>' >&2
  exit 2
fi

revision=$1
archive=/opt/smebot/incoming/${revision}.tar.gz
release=/opt/smebot/releases/${revision}
current=/opt/smebot/current
[[ $(id -un) == smebot-deploy ]]
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

previous=$(readlink -f "$current" || true)
ln -s "$release" /opt/smebot/.next
mv -Tf /opt/smebot/.next "$current"
if ! sudo -n /usr/bin/systemctl restart smebot.service; then
  if [[ -n $previous ]]; then
    ln -s "$previous" /opt/smebot/.next
    mv -Tf /opt/smebot/.next "$current"
    sudo -n /usr/bin/systemctl restart smebot.service || true
  fi
  exit 1
fi
sleep 10
if ! systemctl is-active --quiet smebot.service; then
  if [[ -n $previous ]]; then
    ln -s "$previous" /opt/smebot/.next
    mv -Tf /opt/smebot/.next "$current"
    sudo -n /usr/bin/systemctl restart smebot.service || true
  fi
  echo 'Bot service failed after deployment; previous release restored when available.' >&2
  exit 1
fi
rm -f "$archive"
echo "Activated $revision"
