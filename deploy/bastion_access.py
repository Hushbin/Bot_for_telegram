"""Temporary deployment sessions on a Terraform-managed OCI Bastion."""
from __future__ import annotations

import ipaddress
import re
import time
from typing import Callable

import oci


class BastionAccess:
    """Own one serialized deployment session and restore the idle allowlist."""

    def __init__(self, client, bastion_id: str, idle_cidr: str, session_name: str,
                 target_ip: str, *, sleep: Callable = time.sleep,
                 clock: Callable = time.monotonic):
        network = ipaddress.IPv4Network(idle_cidr, strict=True)
        if network.prefixlen != 32:
            raise ValueError("BOT_BASTION_IDLE_CIDR must be an IPv4 /32")
        ipaddress.IPv4Address(target_ip)
        if not re.fullmatch(r"ocid1\.bastion\.oc1\.[a-z0-9-]+\.[a-z0-9]+", bastion_id):
            raise ValueError("BOT_BASTION_OCID must be a Bastion OCID")
        if not re.fullmatch(r"deploy[0-9]+a[0-9]+", session_name):
            raise ValueError("Invalid deployment session name")
        self.client, self.bastion_id = client, bastion_id
        self.idle_cidr, self.session_name = idle_cidr, session_name
        self.target_ip, self.sleep, self.clock = target_ip, sleep, clock
        self.session_id = None
        self.allowlist_changed = False
        self.create_attempted = False

    def _wait(self, getter, states, *, timeout=120):
        deadline = self.clock() + timeout
        while True:
            result = getter()
            if result.data.lifecycle_state in states:
                return result
            if result.data.lifecycle_state == "FAILED":
                raise RuntimeError("OCI resource entered FAILED state")
            if self.clock() >= deadline:
                raise TimeoutError("OCI resource state did not become ready")
            self.sleep(3)

    def _sessions(self):
        return oci.pagination.list_call_get_all_results(
            self.client.list_sessions, self.bastion_id
        ).data

    def _delete(self, session_id):
        # Retry a lost delete response by inspecting the resource before retrying.
        deadline = self.clock() + 120
        while True:
            try:
                state = self.client.get_session(session_id).data.lifecycle_state
                if state == "DELETED":
                    return
                if state not in ("DELETING", "CREATING"):
                    self.client.delete_session(session_id)
            except oci.exceptions.ServiceError as error:
                # A missing object must not conceal a revoked delete permission.
                # OCI normally retains a DELETED session; require that state.
                if error.status not in (409, 429, 500, 502, 503, 504):
                    raise
            if self.clock() >= deadline:
                raise TimeoutError("Could not verify deployment session deletion")
            self.sleep(3)

    def _allowlist(self, cidr):
        # Retry transient errors and ETag conflicts using a fresh read. The
        # workflow concurrency group is the sole writer for this Bastion.
        deadline = self.clock() + 120
        while True:
            try:
                current = self._wait(lambda: self.client.get_bastion(self.bastion_id), {"ACTIVE"})
                if current.data.max_session_ttl_in_seconds != 3600:
                    raise RuntimeError("Bastion session lifetime must be 3600 seconds")
                if current.data.client_cidr_block_allow_list != [cidr]:
                    self.client.update_bastion(
                        self.bastion_id,
                        oci.bastion.models.UpdateBastionDetails(client_cidr_block_allow_list=[cidr]),
                        if_match=current.headers["etag"],
                    )
                verified = self._wait(lambda: self.client.get_bastion(self.bastion_id), {"ACTIVE"})
                if verified.data.client_cidr_block_allow_list == [cidr]:
                    return
            except oci.exceptions.ServiceError as error:
                if error.status not in (409, 412, 429, 500, 502, 503, 504):
                    raise
            if self.clock() >= deadline:
                raise TimeoutError("Could not verify Bastion client allowlist")
            self.sleep(3)

    def prepare(self, runner_ip):
        runner_cidr = str(ipaddress.IPv4Address(runner_ip)) + "/32"
        current = self.client.get_bastion(self.bastion_id).data
        if current.lifecycle_state != "ACTIVE":
            raise RuntimeError("Deployment Bastion is not ACTIVE")
        if current.max_session_ttl_in_seconds != 3600:
            raise RuntimeError("Bastion session lifetime must be 3600 seconds")
        live = [s for s in self._sessions() if s.lifecycle_state != "DELETED"]
        if any(not re.fullmatch(r"deploy[0-9]+a[0-9]+", s.display_name or "") for s in live):
            raise RuntimeError("Unexpected session on the dedicated deployment Bastion")
        for session in live:
            self._delete(session.id)
        # Mark before the request: the response may be lost after OCI changes it.
        self.allowlist_changed = True
        self._allowlist(runner_cidr)

    def create(self, public_key):
        self.create_attempted = True
        result = self.client.create_session(oci.bastion.models.CreateSessionDetails(
            bastion_id=self.bastion_id, display_name=self.session_name,
            session_ttl_in_seconds=3600,
            key_details=oci.bastion.models.PublicKeyDetails(public_key_content=public_key),
            target_resource_details=oci.bastion.models.CreatePortForwardingSessionTargetResourceDetails(
                session_type="PORT_FORWARDING", target_resource_private_ip_address=self.target_ip,
                target_resource_port=22,
            ),
        ))
        self.session_id = result.data.id
        self._wait(lambda: self.client.get_session(self.session_id), {"ACTIVE"})
        return self.session_id

    def cleanup(self):
        failures = []
        try:
            if self.session_id:
                self._delete(self.session_id)
            elif self.create_attempted:
                # A create timeout can leave a session without a recorded ID.
                deadline = self.clock() + 120
                while True:
                    matches = [s for s in self._sessions() if s.display_name == self.session_name]
                    if matches:
                        for session in matches:
                            if session.lifecycle_state != "DELETED":
                                self._delete(session.id)
                        break
                    if self.clock() >= deadline:
                        # The next serialized run also sweeps all deployment
                        # sessions; expiry remains the final hard-stop fallback.
                        raise RuntimeError("Unable to resolve the session after an ambiguous create")
                    self.sleep(3)
        except Exception:
            failures.append("deployment session cleanup could not be verified")
        try:
            if self.allowlist_changed:
                self._allowlist(self.idle_cidr)
        except Exception:
            failures.append("idle Bastion allowlist could not be restored")
        if failures:
            raise RuntimeError("; ".join(failures))
