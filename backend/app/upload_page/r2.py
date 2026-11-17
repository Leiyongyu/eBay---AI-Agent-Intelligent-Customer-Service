"""Cloudflare R2（兼容 S3）：分块上传签名、合并、拉回、删除。买家浏览器拿着签名地址直接把分块传到 R2，不经过本机。

测试用 set_r2_override() 换成内存里的假存储，不连真实 R2。
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterator

from ..config import get_settings

_override = None


class R2Error(RuntimeError):
    pass


def set_r2_override(obj) -> None:
    global _override
    _override = obj
    _client.cache_clear()


@lru_cache
def _client():
    import boto3
    from botocore.config import Config

    s = get_settings()
    return boto3.client("s3", endpoint_url=s.r2_endpoint_url, aws_access_key_id=s.r2_access_key_id,
                        aws_secret_access_key=s.r2_secret_access_key, region_name="auto",
                        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                                      s3={"addressing_style": "path"}))


class R2:
    def __init__(self):
        self.bucket = get_settings().r2_bucket
        self.c = _client()

    def _call(self, fn, **kw):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return fn(Bucket=self.bucket, **kw)
        except ClientError as e:
            raise R2Error(f"R2 返回错误：{e.response.get('Error', {}).get('Code', 'unknown')}") from None
        except BotoCoreError as e:
            raise R2Error(f"连接 R2 失败：{type(e).__name__}") from None

    def create_multipart(self, key: str, content_type: str) -> str:
        return self._call(self.c.create_multipart_upload, Key=key, ContentType=content_type or "application/octet-stream")["UploadId"]

    def presign_part(self, key: str, upload_id: str, part_number: int, expires: int = 900) -> str:
        return self.c.generate_presigned_url("upload_part", ExpiresIn=expires, HttpMethod="PUT", Params={
            "Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": part_number})

    def list_parts(self, key: str, upload_id: str) -> list[dict]:
        out, marker = [], 0
        while True:
            r = self._call(self.c.list_parts, Key=key, UploadId=upload_id, PartNumberMarker=marker, MaxParts=1000)
            out += [{"part_number": p["PartNumber"], "etag": p["ETag"], "size": p["Size"]} for p in r.get("Parts", [])]
            if not r.get("IsTruncated"):
                return out
            marker = r["NextPartNumberMarker"]

    def complete(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self._call(self.c.complete_multipart_upload, Key=key, UploadId=upload_id, MultipartUpload={
            "Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in sorted(parts, key=lambda x: x["part_number"])]})

    def abort(self, key: str, upload_id: str) -> None:
        try:
            self._call(self.c.abort_multipart_upload, Key=key, UploadId=upload_id)
        except R2Error:
            pass  # 已经被生命周期规则清掉了

    def size(self, key: str) -> int | None:
        try:
            return int(self._call(self.c.head_object, Key=key)["ContentLength"])
        except R2Error:
            return None

    def read(self, key: str, start: int = 0) -> Iterator[bytes]:
        """从 start 字节开始读（续传用）。"""
        kw = {"Range": f"bytes={start}-"} if start else {}
        body = self._call(self.c.get_object, Key=key, **kw)["Body"]
        try:
            yield from body.iter_chunks(chunk_size=1024 * 1024)
        finally:
            body.close()

    def delete(self, key: str) -> None:
        self._call(self.c.delete_object, Key=key)

    def stale_multiparts(self, older_than: datetime) -> list[tuple[str, str]]:
        """开始时间早于 older_than 的未完成分块上传（买家没传完留下的残块）。"""
        r = self._call(self.c.list_multipart_uploads, MaxUploads=1000)
        cut = older_than.replace(tzinfo=timezone.utc) if older_than.tzinfo is None else older_than
        return [(u["Key"], u["UploadId"]) for u in r.get("Uploads", []) if u["Initiated"] < cut]


def r2():
    return _override if _override is not None else R2()
