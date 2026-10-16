#!/usr/bin/env bash
set -euo pipefail
umask 077
exec python "$(dirname "$0")/deploy_via_bastion.py"
