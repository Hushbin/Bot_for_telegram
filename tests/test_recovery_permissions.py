"""Permission probes must not mistake conditional/network errors for IAM denial."""

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import oci

from scripts.verify_recovery_permissions import (
    VerificationError, expect_denied, verify_permissions,
)


def service_error(status, code):
    return oci.exceptions.ServiceError(status, code, {}, "test error")


class RecoveryPermissionTests(unittest.TestCase):
    def test_probe_verifies_restricted_access_without_touching_latest(self):
        objects = {}
        writes = []

        def put_object(namespace, bucket, key, body, **kwargs):
            writes.append((key, kwargs))
            if key in objects:
                raise service_error(404, "NotAuthorizedOrNotFound")
            objects[key] = body

        client = Mock()
        client.put_object.side_effect = put_object
        client.get_object.side_effect = lambda namespace, bucket, key: SimpleNamespace(
            data=SimpleNamespace(content=objects[key]))
        client.list_objects.side_effect = lambda namespace, bucket, **kwargs: SimpleNamespace(
            data=SimpleNamespace(objects=[SimpleNamespace(name=key) for key in objects
                                         if key.startswith(kwargs["prefix"])]))
        client.delete_object.side_effect = service_error(403, "NotAuthorized")
        with redirect_stdout(io.StringIO()):
            keys = verify_permissions(client, "namespace")

        self.assertEqual(set(objects), set(keys))
        self.assertNotIn("latest.json", objects)
        self.assertEqual(client.delete_object.call_count, 2)
        self.assertEqual([kwargs.get("if_none_match") for _, kwargs in writes],
                         ["*", None, "*", None])

    def test_unexpected_write_permission_fails_probe(self):
        with self.assertRaisesRegex(VerificationError, "unexpectedly allowed"):
            expect_denied(lambda: None, "overwrite")

    def test_precondition_authentication_and_server_errors_are_not_permission_proof(self):
        for status, code in ((412, "PreconditionFailed"), (401, "NotAuthenticated"),
                             (500, "InternalServerError"), (404, "BucketNotFound")):
            with self.subTest(status=status, code=code):
                error = service_error(status, code)
                with self.assertRaises(oci.exceptions.ServiceError):
                    expect_denied(Mock(side_effect=error), "overwrite")

    def test_network_error_is_not_permission_proof(self):
        with self.assertRaises(TimeoutError):
            expect_denied(Mock(side_effect=TimeoutError()), "overwrite")


if __name__ == "__main__":
    unittest.main()
