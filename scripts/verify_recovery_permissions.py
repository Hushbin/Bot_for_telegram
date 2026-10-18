"""Probe recovery IAM from the bot VM using disposable, non-user objects."""

import argparse
import json
import sys
from datetime import datetime, timezone
from uuid import uuid4

import oci


BUCKET = "smebot-dev-recovery"


class VerificationError(RuntimeError):
    pass


def expect_denied(operation, description):
    try:
        operation()
    except oci.exceptions.ServiceError as exc:
        if ((exc.status == 404 and exc.code == "NotAuthorizedOrNotFound")
                or (exc.status == 403 and exc.code in ("NotAuthorized", "Forbidden"))):
            return
        raise
    raise VerificationError(f"IAM unexpectedly allowed {description}")


def verify_permissions(client, namespace):
    probe_id = uuid4().hex
    keys = (f"exports/permission-probe-{probe_id}.json",
            f"erasures/0/permission-probe-{probe_id}.json")
    # ID 0 cannot identify a Telegram user. The erasure record is valid for
    # restore replay, and the export probe is not a selectable manifest.
    body = json.dumps({"permission_probe": probe_id, "telegram_user_id": 0,
                       "erased_at": datetime.now(timezone.utc).isoformat()}).encode()
    print(f"probe_id={probe_id}", flush=True)
    for key in keys:
        print(f"cleanup_object={key}", flush=True)
        client.put_object(namespace, BUCKET, key, body, content_type="application/json",
                          content_length=len(body), if_none_match="*")
        if client.get_object(namespace, BUCKET, key).data.content != body:
            raise VerificationError(f"Probe could not be read back: {key}")
        objects = client.list_objects(namespace, BUCKET, prefix=key, limit=1).data.objects
        if not any(item.name == key for item in objects):
            raise VerificationError(f"Probe could not be listed: {key}")
        # No If-None-Match here: denial must come from IAM, independently of
        # the application's conditional-write protection. Reuse harmless bytes
        # so an unexpected grant cannot damage an existing recovery record.
        expect_denied(
            lambda: client.put_object(namespace, BUCKET, key, body,
                                      content_type="application/json", content_length=len(body)),
            f"overwrite of {key}",
        )
        expect_denied(lambda: client.delete_object(namespace, BUCKET, key), f"deletion of {key}")
        if client.get_object(namespace, BUCKET, key).data.content != body:
            raise VerificationError(f"Probe changed during denied operations: {key}")
    print("PASS: create/read/list succeeded; overwrite/delete denied for both probes.")
    print("Operator cleanup is required for the printed objects; latest.json was not touched.")
    return keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        client = oci.object_storage.ObjectStorageClient({}, signer=signer)
        verify_permissions(client, args.namespace)
    except VerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        # SDK exception messages may include request details. Report only the
        # type and service status/code, never object bodies or credentials.
        print(f"Permission probe failed: {type(exc).__name__} "
              f"status={getattr(exc, 'status', None)} code={getattr(exc, 'code', None)}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
