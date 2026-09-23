"""S3 adapter over the two buckets in play (spec §4.2)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

_NOT_FOUND_CODES = {"404", "NoSuchKey"}


class Storage:
    def __init__(self, client: S3Client) -> None:
        self._client = client

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return False
            raise
        return True

    def size(self, bucket: str, key: str) -> int | None:
        """Object size in bytes, or None if it does not exist."""
        try:
            head = self._client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return None
            raise
        return int(head["ContentLength"])

    def download_file(self, bucket: str, key: str, path: Path) -> None:
        self._client.download_file(bucket, key, str(path))

    def upload_file(self, path: Path, bucket: str, key: str, content_type: str) -> None:
        self._client.upload_file(
            str(path), bucket, key, ExtraArgs={"ContentType": content_type}
        )

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType=content_type
        )

    def put_json(self, bucket: str, key: str, obj: object) -> None:
        self.put_bytes(bucket, key, json.dumps(obj).encode("utf-8"), "application/json")

    def get_json(self, bucket: str, key: str) -> Any | None:
        try:
            data = self.get_bytes(bucket, key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return None
            raise
        return json.loads(data)

    def copy(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> None:
        self._client.copy_object(
            Bucket=dst_bucket,
            Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        )

    def list_keys(self, bucket: str, prefix: str) -> list[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    def delete(self, bucket: str, key: str) -> None:
        self._client.delete_object(Bucket=bucket, Key=key)

    def iter_lines(self, bucket: str, key: str) -> Iterator[str]:
        body = self._client.get_object(Bucket=bucket, Key=key)["Body"]
        for line in body.iter_lines():
            yield line.decode("utf-8")
