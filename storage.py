"""Private OCI Object Storage adapter; object keys are never public URLs."""

import io
from uuid import uuid4

import oci


class MediaStorage:
    def __init__(self, settings):
        if settings.oci_auth == "resource_principal":
            signer = oci.auth.signers.get_resource_principals_signer()
            self.client = oci.object_storage.ObjectStorageClient({}, signer=signer)
        elif settings.oci_auth == "instance_principal":
            signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            self.client = oci.object_storage.ObjectStorageClient({}, signer=signer)
        else:
            config = oci.config.from_file(
                file_location=settings.oci_config_file,
                profile_name=settings.oci_profile,
            )
            self.client = oci.object_storage.ObjectStorageClient(config)
        self.namespace = settings.oci_namespace
        self.bucket = settings.oci_bucket_name

    def put_product_photo(self, seller_id: int, product_id: int, photo: bytes):
        if not photo or len(photo) > 5 * 1024 * 1024:
            raise ValueError("Photo must be between 1 byte and 5 MB")
        if not (photo.startswith(b"\xff\xd8\xff") or photo.startswith(b"\x89PNG\r\n\x1a\n")):
            raise ValueError("Only JPEG or PNG photos are accepted")
        extension = "jpg" if photo.startswith(b"\xff\xd8\xff") else "png"
        content_type = "image/jpeg" if extension == "jpg" else "image/png"
        object_key = f"products/{seller_id}/{product_id}/{uuid4().hex}.{extension}"
        response = self.client.put_object(
            self.namespace, self.bucket, object_key, io.BytesIO(photo),
            content_length=len(photo), content_type=content_type,
            if_none_match="*",
        )
        return object_key, response.headers.get("version-id")

    def get_photo(self, object_key: str) -> bytes:
        response = self.client.get_object(self.namespace, self.bucket, object_key)
        return response.data.content

    def delete_version(self, object_key: str, version_id: str) -> None:
        # Deleting a specific version removes its bytes from a versioned bucket.
        self.client.delete_object(
            self.namespace, self.bucket, object_key, version_id=version_id
        )
