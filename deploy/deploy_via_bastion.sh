#!/usr/bin/env bash
set -euo pipefail
umask 077

for name in OCI_USER_OCID OCI_TENANCY_OCID OCI_FINGERPRINT OCI_API_PRIVATE_KEY BOT_DEPLOY_SSH_PRIVATE_KEY BOT_COMPARTMENT_OCID BOT_BASTION_SUBNET_OCID BOT_PRIVATE_IP BOT_SSH_HOST_KEY GITHUB_SHA GITHUB_RUN_ID GITHUB_RUN_ATTEMPT RUNNER_TEMP OCI_REGION; do
  if [[ -z ${!name:-} ]]; then
    echo "Missing $name in the dev-bot environment" >&2
    exit 2
  fi
done
[[ $GITHUB_SHA =~ ^[0-9a-f]{40}$ ]]
[[ $OCI_REGION =~ ^[a-z0-9-]+$ ]]
[[ $GITHUB_RUN_ID =~ ^[0-9]+$ && $GITHUB_RUN_ATTEMPT =~ ^[0-9]+$ ]]

work=$(mktemp -d)
bastion_id=
bastion_name=
session_id=
tunnel_pid=
cleanup() {
  if [[ -n $tunnel_pid ]]; then kill "$tunnel_pid" 2>/dev/null || true; fi
  if [[ -n $session_id ]]; then
    oci bastion session delete --session-id "$session_id" --force --wait-for-state SUCCEEDED || true
  fi
  if [[ -z $bastion_id && -n $bastion_name ]]; then
    bastion_id=$(oci bastion bastion list --compartment-id "$BOT_COMPARTMENT_OCID" \
      --all 2>/dev/null | python -c \
      'import json, os, sys; matches=[x["id"] for x in json.load(sys.stdin)["data"] if x["name"] == os.environ["BASTION_NAME"]]; print(matches[0] if len(matches) == 1 else "")' 2>/dev/null) || true
  fi
  if [[ -n $bastion_id ]]; then
    oci bastion bastion delete --bastion-id "$bastion_id" --force --wait-for-state SUCCEEDED || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

install -d -m 0700 "$work/.oci"
printf '%s\n' "$OCI_API_PRIVATE_KEY" > "$work/.oci/api_key.pem"
printf '%s\n' "$BOT_DEPLOY_SSH_PRIVATE_KEY" > "$work/vm_key"
cat > "$work/.oci/config" <<EOF
[DEFAULT]
user=$OCI_USER_OCID
tenancy=$OCI_TENANCY_OCID
fingerprint=$OCI_FINGERPRINT
key_file=$work/.oci/api_key.pem
region=$OCI_REGION
EOF
export OCI_CLI_CONFIG_FILE="$work/.oci/config"

runner_ip=$(curl -4fsS --max-time 15 https://checkip.amazonaws.com | tr -d '\r\n')
[[ $runner_ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
client_cidr="[\"${runner_ip}/32\"]"
bastion_name="smebotdeploy${GITHUB_RUN_ID}a${GITHUB_RUN_ATTEMPT}"
export BASTION_NAME="$bastion_name"
oci bastion bastion create \
  --bastion-type standard \
  --compartment-id "$BOT_COMPARTMENT_OCID" \
  --target-subnet-id "$BOT_BASTION_SUBNET_OCID" \
  --client-cidr-list "$client_cidr" \
  --max-session-ttl 3600 \
  --name "$bastion_name" \
  --wait-for-state SUCCEEDED >/dev/null
# Create returns a work-request OCID; list the active Bastion by its unique name.
bastion_id=$(oci bastion bastion list --compartment-id "$BOT_COMPARTMENT_OCID" \
  --bastion-lifecycle-state ACTIVE --all | python -c \
  'import json, os, sys; matches=[x["id"] for x in json.load(sys.stdin)["data"] if x["name"] == os.environ["BASTION_NAME"]]; print(matches[0] if len(matches) == 1 else "")')
[[ $bastion_id == ocid1.bastion.* ]]

ssh-keygen -q -t ed25519 -N '' -f "$work/bastion_key"
session_name="deploy${GITHUB_RUN_ID}a${GITHUB_RUN_ATTEMPT}"
oci bastion session create-port-forwarding \
  --bastion-id "$bastion_id" \
  --ssh-public-key-file "$work/bastion_key.pub" \
  --target-private-ip "$BOT_PRIVATE_IP" \
  --target-port 22 \
  --session-ttl 3600 \
  --display-name "$session_name" \
  --wait-for-state SUCCEEDED >/dev/null
export SESSION_NAME="$session_name"
session_id=$(oci bastion session list --bastion-id "$bastion_id" \
  --session-lifecycle-state ACTIVE --all | python -c \
  'import json, os, sys; matches=[x["id"] for x in json.load(sys.stdin)["data"] if x["display-name"] == os.environ["SESSION_NAME"]]; print(matches[0] if len(matches) == 1 else "")')
[[ $session_id == ocid1.bastionsession.* ]]

# OCI's per-session identity restricts the tunnel to this VM and port.
ssh -i "$work/bastion_key" -o StrictHostKeyChecking=accept-new \
  -o UserKnownHostsFile="$work/bastion_known_hosts" \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -N -L 127.0.0.1:2222:"$BOT_PRIVATE_IP":22 -p 22 \
  "${session_id}@host.bastion.${OCI_REGION}.oci.oraclecloud.com" &
tunnel_pid=$!

# The VM key is pinned separately, so the deployment authenticates the target.
printf '[127.0.0.1]:2222 %s\n' "$BOT_SSH_HOST_KEY" > "$work/vm_known_hosts"
ssh_opts=(-i "$work/vm_key" -p 2222 -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$work/vm_known_hosts" -o BatchMode=yes -o ConnectTimeout=10)
for attempt in {1..20}; do
  if ssh "${ssh_opts[@]}" smebot-deploy@127.0.0.1 true 2>/dev/null; then break; fi
  if (( attempt == 20 )); then echo 'Bastion tunnel or VM SSH did not become ready' >&2; exit 1; fi
  sleep 3
done

archive="$RUNNER_TEMP/${GITHUB_SHA}.tar.gz"
hash=$(cat "$RUNNER_TEMP/release.sha256")
scp -i "$work/vm_key" -P 2222 -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$work/vm_known_hosts" -o BatchMode=yes \
  "$archive" "smebot-deploy@127.0.0.1:/opt/smebot/incoming/${GITHUB_SHA}.tar.gz"
ssh "${ssh_opts[@]}" smebot-deploy@127.0.0.1 \
  "/usr/local/sbin/smebot-activate '$GITHUB_SHA' '$hash'"
