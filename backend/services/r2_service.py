"""Cloudflare R2 object storage service – pre-signed URL generation.

Used for the hybrid architecture where large PDF files are uploaded
directly to R2 (bypassing Railway) while API keys and control flow
remain on the Railway backend.

Flow:
  1. Client requests a pre-signed upload URL from Railway.
  2. Client uploads the PDF directly to R2 (no Railway traffic).
  3. Client tells Railway "file is at this R2 key, please parse it".
  4. Railway downloads from R2 → sends to Upstage Document Parse.
  5. Railway returns HTML/Markdown results to the client.

R2 is S3-compatible, so we use boto3 with a custom endpoint.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger(__name__)

# Maximum pre-signed URL validity (seconds)
_DEFAULT_UPLOAD_EXPIRY = 3600  # 1 hour
_DEFAULT_DOWNLOAD_EXPIRY = 3600  # 1 hour

# Maximum upload size (bytes): 5 GB
_MAX_UPLOAD_SIZE = 5 * 1024 * 1024 * 1024


@dataclass
class R2Config:
    """Cloudflare R2 configuration (loaded from environment variables)."""
    account_id: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    bucket_name: str = ""
    public_url: str = ""  # Optional: custom domain for public reads

    @classmethod
    def from_env(cls) -> "R2Config":
        return cls(
            account_id=os.environ.get("R2_ACCOUNT_ID", ""),
            access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
            secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
            bucket_name=os.environ.get("R2_BUCKET_NAME", "moa-documents"),
            public_url=os.environ.get("R2_PUBLIC_URL", ""),
        )

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @property
    def is_configured(self) -> bool:
        return bool(
            self.account_id
            and self.access_key_id
            and self.secret_access_key
            and self.bucket_name
        )


class R2Service:
    """Cloudflare R2 object storage operations."""

    def __init__(self, config: R2Config | None = None):
        self.config = config or R2Config.from_env()
        self._client = None

    @property
    def client(self):
        """Lazy-initialized boto3 S3 client for R2."""
        if self._client is None:
            if not self.config.is_configured:
                raise RuntimeError(
                    "R2 not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                    "R2_SECRET_ACCESS_KEY, and R2_BUCKET_NAME environment variables."
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint_url,
                aws_access_key_id=self.config.access_key_id,
                aws_secret_access_key=self.config.secret_access_key,
                config=BotoConfig(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                ),
                region_name="auto",
            )
        return self._client

    # ------------------------------------------------------------------
    # Pre-signed URL generation
    # ------------------------------------------------------------------

    def generate_upload_url(
        self,
        user_id: str,
        filename: str,
        content_type: str = "application/pdf",
        expiry_seconds: int = _DEFAULT_UPLOAD_EXPIRY,
        max_size_bytes: int = _MAX_UPLOAD_SIZE,
    ) -> dict:
        """Generate a pre-signed URL for direct file upload to R2.

        Returns:
            {
                "upload_url": str,       # PUT this URL with the file body
                "object_key": str,       # R2 object key for later reference
                "expiry_seconds": int,
                "max_size_bytes": int,
                "method": "PUT",
            }
        """
        # Generate a unique object key with user isolation
        timestamp = int(time.time())
        unique_id = uuid.uuid4().hex[:12]
        safe_filename = filename.replace("/", "_").replace("\\", "_")
        object_key = f"uploads/{user_id}/{timestamp}_{unique_id}/{safe_filename}"

        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.config.bucket_name,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=expiry_seconds,
        )

        logger.info(
            "Generated R2 upload URL for user=%s file=%s key=%s",
            user_id, filename, object_key,
        )

        return {
            "upload_url": url,
            "object_key": object_key,
            "expiry_seconds": expiry_seconds,
            "max_size_bytes": max_size_bytes,
            "method": "PUT",
        }

    def generate_download_url(
        self,
        object_key: str,
        expiry_seconds: int = _DEFAULT_DOWNLOAD_EXPIRY,
    ) -> str:
        """Generate a pre-signed URL for downloading an object from R2."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.config.bucket_name,
                "Key": object_key,
            },
            ExpiresIn=expiry_seconds,
        )

    # ------------------------------------------------------------------
    # Object operations
    # ------------------------------------------------------------------

    def download_object(self, object_key: str) -> bytes:
        """Download an object from R2 and return its content as bytes."""
        response = self.client.get_object(
            Bucket=self.config.bucket_name,
            Key=object_key,
        )
        return response["Body"].read()

    def download_to_file(self, object_key: str, local_path: str) -> None:
        """Download an R2 object to a local file."""
        self.client.download_file(
            self.config.bucket_name,
            object_key,
            local_path,
        )
        logger.info("Downloaded R2 object %s → %s", object_key, local_path)

    def delete_object(self, object_key: str) -> None:
        """Delete an object from R2."""
        self.client.delete_object(
            Bucket=self.config.bucket_name,
            Key=object_key,
        )
        logger.info("Deleted R2 object: %s", object_key)

    def object_exists(self, object_key: str) -> bool:
        """Check if an object exists in R2."""
        try:
            self.client.head_object(
                Bucket=self.config.bucket_name,
                Key=object_key,
            )
            return True
        except self.client.exceptions.ClientError:
            return False
        except Exception:
            return False

    def get_object_size(self, object_key: str) -> int:
        """Get the size of an R2 object in bytes."""
        response = self.client.head_object(
            Bucket=self.config.bucket_name,
            Key=object_key,
        )
        return response.get("ContentLength", 0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_user_uploads(
        self,
        user_id: str,
        older_than_seconds: int = 86400,  # 24 hours
    ) -> int:
        """Delete old upload objects for a user.

        Returns the number of objects deleted.
        """
        prefix = f"uploads/{user_id}/"
        cutoff = time.time() - older_than_seconds
        deleted = 0

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.config.bucket_name,
                Prefix=prefix,
            ):
                for obj in page.get("Contents", []):
                    last_modified = obj.get("LastModified")
                    if last_modified and last_modified.timestamp() < cutoff:
                        self.client.delete_object(
                            Bucket=self.config.bucket_name,
                            Key=obj["Key"],
                        )
                        deleted += 1
        except Exception as exc:
            logger.warning("R2 cleanup failed for user %s: %s", user_id, exc)

        if deleted > 0:
            logger.info("Cleaned up %d old R2 objects for user %s", deleted, user_id)

        return deleted

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Check if R2 is configured and accessible."""
        if not self.config.is_configured:
            return False
        try:
            self.client.head_bucket(Bucket=self.config.bucket_name)
            return True
        except Exception:
            return False
