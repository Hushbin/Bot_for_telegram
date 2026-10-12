import unittest
from types import SimpleNamespace
from unittest.mock import patch

from storage import MediaStorage


class OCIStorageTests(unittest.TestCase):
    def test_instance_principal_initializes_object_storage_client(self):
        settings = SimpleNamespace(
            oci_auth="instance_principal",
            oci_namespace="example",
            oci_bucket_name="media",
        )
        with patch("storage.oci.auth.signers.InstancePrincipalsSecurityTokenSigner") as signer, \
             patch("storage.oci.object_storage.ObjectStorageClient") as client:
            media = MediaStorage(settings)
        signer.assert_called_once_with()
        client.assert_called_once_with({}, signer=signer.return_value)
        self.assertEqual(media.namespace, "example")
        self.assertEqual(media.bucket, "media")
