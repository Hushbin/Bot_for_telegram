#!/usr/bin/env bash
set -euo pipefail

# Run once on the bot VM as root: bash bootstrap_vm.sh /path/to/deploy.pub
if (( EUID != 0 )) || (( $# != 1 )); then
  echo "Usage: sudo bash bootstrap_vm.sh /path/to/deploy.pub" >&2
  exit 2
fi

deploy_key=$1
grep -Eq '^ssh-(ed25519|rsa) [A-Za-z0-9+/=]+( .*)?$' "$deploy_key"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
id smebot >/dev/null 2>&1 || useradd --system --user-group --home-dir /opt/smebot --shell /usr/sbin/nologin smebot
id smebot-deploy >/dev/null 2>&1 || useradd --system --user-group --home-dir /opt/smebot --shell /bin/bash smebot-deploy

install -d -m 0755 -o smebot-deploy -g smebot-deploy /opt/smebot
install -d -m 0755 -o smebot-deploy -g smebot-deploy /opt/smebot/releases /opt/smebot/incoming
install -d -m 0750 -o root -g smebot /etc/smebot /etc/smebot/wallet
install -d -m 0700 -o smebot-deploy -g smebot-deploy /opt/smebot/.ssh
install -m 0600 -o smebot-deploy -g smebot-deploy "$deploy_key" /opt/smebot/.ssh/authorized_keys
install -m 0755 -o root -g root "$(dirname "$0")/activate_release.sh" /usr/local/sbin/smebot-activate
install -m 0644 -o root -g root "$(dirname "$0")/smebot.service" /etc/systemd/system/smebot.service

cat >/etc/sudoers.d/smebot-deploy <<'EOF'
smebot-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart smebot.service
EOF
chmod 0440 /etc/sudoers.d/smebot-deploy
visudo -cf /etc/sudoers.d/smebot-deploy
systemctl daemon-reload
systemctl enable smebot.service
echo 'Install /etc/smebot/bot.env and the two wallet files, then run the deployment workflow.'
