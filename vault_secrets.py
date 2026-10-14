"""Fetch VM runtime credentials with the OCI instance principal at startup."""

import base64

import oci


def load_vault_secrets(secret_ids: dict[str, str]) -> dict[str, str]:
    """Return current secret values without caching them on disk or logging contents."""
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.secrets.SecretsClient({}, signer=signer)
        values = {}
        for name, secret_id in secret_ids.items():
            bundle = client.get_secret_bundle(secret_id, stage="CURRENT").data
            content = bundle.secret_bundle_content.content
            value = base64.b64decode(content, validate=True).decode("utf-8")
            if not value or "\x00" in value:
                raise ValueError("Invalid secret content")
            values[name] = value
        return values
    except Exception:
        # OCI errors can include request details. Never put them, or secret values,
        # into a startup traceback or a deployment log.
        raise RuntimeError("Unable to load runtime secrets from OCI Vault") from None
