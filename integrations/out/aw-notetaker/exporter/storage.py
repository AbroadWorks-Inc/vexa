"""S3 adapter over the two buckets in play (spec §4.2)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

_NOT_FOUND_CODES = {"404", "NoSuchKey"}
# A conditional write lost: the object changed, appeared or went since it was
# read (412), or another conditional write on it was in flight (409).
_CONFLICT_CODES = {"412", "PreconditionFailed", "409", "ConditionalRequestConflict"}


class PreconditionFailed(Exception):
    """A conditional write found the object no longer as it was read."""


def _tagging(retention: str) -> str:
    """URL-encoded `Tagging` form for a `retention-class` value (spec §3/§7)."""
    return urlencode({"retention-class": retention})


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

    def upload_file(
        self,
        path: Path,
        bucket: str,
        key: str,
        content_type: str,
        retention: str | None = None,
    ) -> None:
        extra_args: dict[str, str] = {"ContentType": content_type}
        if retention is not None:
            extra_args["Tagging"] = _tagging(retention)
        self._client.upload_file(str(path), bucket, key, ExtraArgs=extra_args)

    def get_bytes(self, bucket: str, key: str) -> bytes:
        return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str,
        retention: str | None = None,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        """Write the object. With `if_match` (an ETag) only over that version,
        with `if_none_match` only where there is none; otherwise
        `PreconditionFailed`."""
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if retention is not None:
            kwargs["Tagging"] = _tagging(retention)
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        try:
            self._client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _CONFLICT_CODES or (
                if_match is not None and code in _NOT_FOUND_CODES
            ):
                raise PreconditionFailed(f"{bucket}/{key}: {code}") from exc
            raise

    def put_json(
        self,
        bucket: str,
        key: str,
        obj: object,
        retention: str | None = None,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> None:
        self.put_bytes(
            bucket,
            key,
            json.dumps(obj).encode("utf-8"),
            "application/json",
            retention=retention,
            if_match=if_match,
            if_none_match=if_none_match,
        )

    def get_json(self, bucket: str, key: str) -> Any | None:
        try:
            data = self.get_bytes(bucket, key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return None
            raise
        return json.loads(data)

    def get_json_versioned(self, bucket: str, key: str) -> tuple[Any, str] | None:
        """The object's JSON and the ETag of that version, or None if it does
        not exist."""
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return None
            raise
        return json.loads(resp["Body"].read()), resp["ETag"]

    def copy(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
        retention: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {
            "Bucket": dst_bucket,
            "Key": dst_key,
            "CopySource": {"Bucket": src_bucket, "Key": src_key},
        }
        if retention is not None:
            kwargs["TaggingDirective"] = "REPLACE"
            kwargs["Tagging"] = _tagging(retention)
        self._client.copy_object(**kwargs)

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
