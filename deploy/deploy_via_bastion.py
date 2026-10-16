"""Build a private SSH tunnel, transfer a release, and activate it."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

import oci

from bastion_access import BastionAccess


REQUIRED = (
    "OCI_USER_OCID", "OCI_TENANCY_OCID", "OCI_FINGERPRINT", "OCI_API_PRIVATE_KEY",
    "BOT_DEPLOY_SSH_PRIVATE_KEY", "BOT_BASTION_OCID", "BOT_BASTION_IDLE_CIDR",
    "BOT_PRIVATE_IP", "BOT_SSH_HOST_KEY", "GITHUB_SHA", "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT", "RUNNER_TEMP", "OCI_REGION",
)


def deploy(env, directory, session_id):
    directory = Path(directory)
    vm_key, tunnel_key = directory / "vm_key", directory / "bastion_key"
    vm_key.write_text(env["BOT_DEPLOY_SSH_PRIVATE_KEY"] + "\n")
    vm_key.chmod(0o600)
    known_hosts = directory / "vm_known_hosts"
    known_hosts.write_text("[127.0.0.1]:2222 " + env["BOT_SSH_HOST_KEY"] + "\n")
    options = ["-i", str(vm_key), "-o", "StrictHostKeyChecking=yes", "-o",
               "UserKnownHostsFile=" + str(known_hosts), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    tunnel = None
    try:
        for attempt in range(30):
            tunnel = subprocess.Popen([
                "ssh", "-i", str(tunnel_key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", "-o",
                "UserKnownHostsFile=" + str(directory / "bastion_known_hosts"),
                "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
                "-N", "-L", "127.0.0.1:2222:" + env["BOT_PRIVATE_IP"] + ":22", "-p", "22",
                session_id + "@host.bastion." + env["OCI_REGION"] + ".oci.oraclecloud.com",
            ])
            time.sleep(3)
            if tunnel.poll() is None and subprocess.run(
                ["ssh", *options, "-p", "2222", "smebot-deploy@127.0.0.1", "true"],
                stderr=subprocess.DEVNULL,
            ).returncode == 0:
                break
            tunnel.terminate()
            tunnel.wait(timeout=15)
            tunnel = None
            if attempt == 29:
                raise RuntimeError("Bastion tunnel or VM SSH did not become ready")
        archive = Path(env["RUNNER_TEMP"]) / (env["GITHUB_SHA"] + ".tar.gz")
        digest = (Path(env["RUNNER_TEMP"]) / "release.sha256").read_text().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid release digest")
        subprocess.run(["scp", *options, "-P", "2222", str(archive),
                        "smebot-deploy@127.0.0.1:/opt/smebot/incoming/" + archive.name], check=True)
        command = shlex.join(["/usr/local/sbin/smebot-activate", env["GITHUB_SHA"], digest])
        subprocess.run(["ssh", *options, "-p", "2222", "smebot-deploy@127.0.0.1", command], check=True)
    finally:
        if tunnel is not None:
            tunnel.terminate()
            tunnel.wait(timeout=15)


def run(env, *, client_factory=oci.bastion.BastionClient, transfer=deploy):
    for name in REQUIRED:
        if not env.get(name):
            raise ValueError("Missing " + name + " in the dev-bot environment")
    for name, pattern in [("GITHUB_SHA", r"[0-9a-f]{40}"), ("OCI_REGION", r"[a-z0-9-]+"),
                          ("GITHUB_RUN_ID", r"[0-9]+"), ("GITHUB_RUN_ATTEMPT", r"[0-9]+")]:
        if not re.fullmatch(pattern, env[name]):
            raise ValueError("Invalid " + name)
    client = client_factory({
        "user": env["OCI_USER_OCID"], "tenancy": env["OCI_TENANCY_OCID"],
        "fingerprint": env["OCI_FINGERPRINT"], "key_content": env["OCI_API_PRIVATE_KEY"],
        "region": env["OCI_REGION"],
    }, timeout=(10, 30))
    access = BastionAccess(client, env["BOT_BASTION_OCID"], env["BOT_BASTION_IDLE_CIDR"],
                           "deploy" + env["GITHUB_RUN_ID"] + "a" + env["GITHUB_RUN_ATTEMPT"],
                           env["BOT_PRIVATE_IP"])
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=15) as response:
        runner_ip = response.read(128).decode("ascii").strip()
    try:
        with tempfile.TemporaryDirectory(prefix="smebot-deploy-") as directory:
            access.prepare(runner_ip)
            key = Path(directory) / "bastion_key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
            session_id = access.create(key.with_suffix(".pub").read_text().strip())
            transfer(env, directory, session_id)
    finally:
        access.cleanup()


def main():
    os.umask(0o077)
    def interrupted(signum, frame):
        # Avoid a second signal interrupting cleanup itself.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise InterruptedError("Deployment interrupted; cleaning up")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        run(os.environ)
    except oci.exceptions.ServiceError as error:
        print("OCI deployment request failed: HTTP " + str(error.status) + " " + error.code, file=sys.stderr)
        return 1
    except Exception as error:
        # Avoid SDK tracebacks/configuration dumps containing credential data.
        print("Deployment failed: " + str(error) if isinstance(error, (ValueError, RuntimeError, InterruptedError))
              else "Deployment failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
