"""对象存储后端（YOUJU_STORAGE_BACKEND）：cloudbase 云托管对象存储（现在）、disk 服务器本机目录、
s3 兼容 S3 的对象存储（2.0 自建服务器）。

附件副本、批量工具结果、数据库备份、访问日志归档、公开快照这几处的代码都是照云托管的写法写的：
经开放接口服务调 /tcb/uploadfile、/tcb/batchdownloadfile、/tcb/batchdeletefile，文件 ID 写成
cloud://环境.存储桶/路径，读文件用换来的 https 临时链接（可以带 Range 分段读）。这些代码处理了
很多边界情况，所以 2.0 不改它们，只在最底下换掉实现：

- backup._open_api 在 disk、s3 时改调 open_api()：按原接口的请求和返回格式，在本机目录或 S3 上做；
- backup.upload 在 disk、s3 时改调 upload()：直接写文件，不走云托管的两步上传；
- 临时链接：s3 是真的预签名链接（https，按 max_age 过期）；disk 是 https://storage.youju.invalid/o/路径，
  只有 http_client() 建的客户端认得（挂了 DiskTransport，在进程里直接读文件、支持 Range），
  .invalid 是保留域名，别的客户端拿到也连不出去；
- storage_list 在 disk、s3 时改调 list_objects()（云托管要先取临时密钥再用 COS 接口列）。

文件 ID 仍写成 cloud://<YOUJU_CLOUD_ENV>.<YOUJU_STORAGE_BUCKET>/<路径>：数据库里的旧文件 ID 不用改，
切换时把云托管存储桶里的文件按原路径拷过来就能用（docs/部署/自建服务器.md）。认文件 ID 时只看
"cloud://环境.随便什么/" 后面的路径，所以存储桶名不一样的旧 ID 也能读、能删。

默认 cloudbase：上面几处全部走原来的代码，http_client() 就是 httpx.Client(trust_env=False)。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from youju.config import Settings, get_settings

FAKE_ORIGIN = "https://storage.youju.invalid"
_OBJECT_PREFIX = "/o/"
# 路径：一段一段的字母、数字和 . _ -，不能有空段、"." 和 ".."
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,200}")
_FILE_ID = re.compile(r"cloud://[^./]+\.[^/]+/(.+)")
# 云托管存储接口在文件不存在时给的状态码（平台的值不清楚，这里只要是非 0 就行）
_MISSING = -503003
_CHUNK = 1024 * 1024


class StorageError(RuntimeError):
    """存储出错；信息里不带密钥、签名和文件内容。"""


def emulated(settings: Settings | None = None) -> bool:
    """不是云托管对象存储（disk、s3）：几处底层调用改走本模块。"""
    return getattr(settings or get_settings(), "storage_backend", "cloudbase") != "cloudbase"


def valid_path(path: str) -> bool:
    if not path or len(path) > 900 or path.startswith("/") or path.endswith("/"):
        return False
    return all(_SEGMENT.fullmatch(p) and p not in (".", "..") for p in path.split("/"))


def path_of(file_id: str | None) -> str | None:
    """文件 ID（cloud://环境.存储桶/路径）里的路径；格式不对返回 None。"""
    match = _FILE_ID.fullmatch(file_id or "")
    if not match or not valid_path(match.group(1)):
        return None
    return match.group(1)


def file_id(env: str, path: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"cloud://{env}.{settings.storage_bucket}/{path}"


@dataclass(frozen=True)
class StoredObject:
    key: str
    last_modified: datetime
    size: int = 0


class Backend(Protocol):
    def put_file(
        self, path: str, local: Path, *, headers: dict[str, str] | None = None
    ) -> None: ...
    def exists(self, path: str) -> bool: ...
    def delete(self, path: str) -> None: ...
    def url(self, path: str, max_age: int) -> str: ...
    def list(self, prefix: str) -> list[StoredObject]: ...


# ---- 服务器本机目录 ----


class DiskBackend:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _file(self, path: str) -> Path:
        if not valid_path(path):
            raise StorageError("文件路径不合规")
        return self.root / path

    def put_file(self, path: str, local: Path, *, headers: dict[str, str] | None = None) -> None:
        target = self._file(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名：读的一方不会读到写了一半的文件；同一路径重写就是覆盖（同云托管）
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out, Path(local).open("rb") as src:
                while chunk := src.read(_CHUNK):
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def exists(self, path: str) -> bool:
        return self._file(path).is_file()

    def delete(self, path: str) -> None:
        self._file(path).unlink(missing_ok=True)

    def url(self, path: str, max_age: int) -> str:
        self._file(path)
        return FAKE_ORIGIN + _OBJECT_PREFIX + quote(path, safe="/")

    def list(self, prefix: str) -> list[StoredObject]:
        found = []
        base = self.root
        if not base.is_dir():
            return []
        for file in base.rglob("*"):
            if not file.is_file() or file.name.startswith(".upload-"):
                continue
            key = file.relative_to(base).as_posix()
            if key.startswith(prefix):
                stat = file.stat()
                found.append(
                    StoredObject(key, datetime.fromtimestamp(stat.st_mtime, UTC), stat.st_size)
                )
        return sorted(found, key=lambda item: item.key)

    def open(self, path: str):
        return self._file(path).open("rb")


class DiskTransport(httpx.BaseTransport):
    """https://storage.youju.invalid/o/<路径> 的 GET、HEAD：直接读本机目录，支持单段 Range。"""

    def __init__(self, backend: DiskBackend) -> None:
        self.backend = backend

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().split("?", 1)[0]
        if request.method not in ("GET", "HEAD") or not raw.startswith(_OBJECT_PREFIX):
            return httpx.Response(405, request=request)
        path = unquote(raw[len(_OBJECT_PREFIX) :])
        if not valid_path(path) or not self.backend.exists(path):
            return httpx.Response(404, request=request)
        file = self.backend._file(path)
        total = file.stat().st_size
        start, end, status = 0, total - 1, 200
        spec = request.headers.get("range")
        if spec:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", spec.strip())
            if not match or total == 0:
                return httpx.Response(416, headers={"content-range": f"bytes */{total}"})
            start = int(match.group(1))
            end = min(int(match.group(2)) if match.group(2) else total - 1, total - 1)
            if start > end:
                return httpx.Response(416, headers={"content-range": f"bytes */{total}"})
            status = 206
        length = end - start + 1 if total else 0
        headers = {"content-length": str(length)}
        if status == 206:
            headers["content-range"] = f"bytes {start}-{end}/{total}"
        if request.method == "HEAD":
            return httpx.Response(status, headers=headers, request=request)
        # 按块读，不整个读进内存（整库备份几百 MB，worker 只有 640MB）
        return httpx.Response(
            status, headers=headers, stream=_FileRange(file, start, length), request=request
        )


class _FileRange(httpx.SyncByteStream):
    def __init__(self, file: Path, start: int, length: int) -> None:
        self.file, self.start, self.length = file, start, length

    def __iter__(self) -> Iterator[bytes]:
        left = self.length
        with self.file.open("rb") as fh:
            fh.seek(self.start)
            while left > 0:
                chunk = fh.read(min(_CHUNK, left))
                if not chunk:
                    return
                left -= len(chunk)
                yield chunk


# ---- 兼容 S3 的对象存储（AWS 签名 V4；腾讯云 COS、MinIO 都认） ----


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def _uri_encode(text: str, *, keep_slash: bool) -> str:
    return quote(text, safe="-_.~/" if keep_slash else "-_.~")


def _canonical_query(query: dict[str, str]) -> str:
    return "&".join(
        f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
        for k, v in sorted(query.items())
    )


@dataclass(frozen=True)
class S3Config:
    endpoint: str  # https://cos.ap-shanghai.myqcloud.com
    region: str
    bucket: str
    access_key: str
    secret_key: str
    path_style: bool = False

    def __repr__(self) -> str:  # 不把密钥打进日志
        return f"S3Config(endpoint={self.endpoint!r}, bucket={self.bucket!r})"


def sigv4_headers(
    config: S3Config,
    method: str,
    host: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_hash: str,
    now: datetime,
) -> dict[str, str]:
    """按 AWS 签名 V4 给请求签名，返回要加的头（含 Authorization）。path 已按 URI 编码。"""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    all_headers = {k.lower(): str(v).strip() for k, v in headers.items()}
    all_headers.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
    signed = ";".join(sorted(all_headers))
    canonical_headers = "".join(f"{k}:{all_headers[k]}\n" for k in sorted(all_headers))
    canonical = "\n".join(
        [method, path, _canonical_query(query), canonical_headers, signed, payload_hash]
    )
    scope = f"{day}/{config.region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _hash(canonical.encode())])
    key = _hmac(("AWS4" + config.secret_key).encode(), day)
    for part in (config.region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in all_headers.items() if k != "host"}
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={config.access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return out


def presign_url(
    config: S3Config, host: str, path: str, expires: int, now: datetime, scheme: str = "https"
) -> str:
    """预签名的 GET 链接（只签 host），expires 秒后失效。path 已按 URI 编码。"""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    scope = f"{day}/{config.region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{config.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = _canonical_query(query)
    canonical = "\n".join(
        ["GET", path, canonical_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"]
    )
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _hash(canonical.encode())])
    key = _hmac(("AWS4" + config.secret_key).encode(), day)
    for part in (config.region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class S3Backend:
    def __init__(self, config: S3Config, *, http: httpx.Client | None = None) -> None:
        self.config = config
        parts = urlsplit(config.endpoint)
        self.scheme, self.endpoint_host = parts.scheme or "https", parts.netloc
        self._http = http

    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(trust_env=False, timeout=60)

    def _where(self, key: str) -> tuple[str, str]:
        """（主机名, 按 URI 编码的路径）。key 为空是存储桶本身（列文件用）。"""
        encoded = _uri_encode(key, keep_slash=True)
        if self.config.path_style:
            return self.endpoint_host, f"/{self.config.bucket}/{encoded}"
        return f"{self.config.bucket}.{self.endpoint_host}", f"/{encoded}"

    def _request(
        self,
        method: str,
        key: str,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | Iterator[bytes] = b"",
        payload_hash: str | None = None,
        now: datetime | None = None,
    ) -> httpx.Response:
        host, path = self._where(key)
        query = query or {}
        payload_hash = payload_hash or _hash(body if isinstance(body, bytes) else b"")
        signed = sigv4_headers(
            self.config,
            method,
            host,
            path,
            query,
            headers or {},
            payload_hash,
            now or datetime.now(UTC),
        )
        # 查询串按签名时的写法拼好（httpx 自己编码的写法可能和签名的不一样）
        url = f"{self.scheme}://{host}{path}" + ("?" + _canonical_query(query) if query else "")
        client = self._client()
        try:
            request = client.build_request(method, url, headers=signed, content=body)
            response = client.send(request)
            response.read()
            return response
        except httpx.HTTPError as exc:
            raise StorageError(f"对象存储请求失败：{type(exc).__name__}") from None
        finally:
            if self._http is None:
                client.close()

    def _check(self, response: httpx.Response, what: str, ok: tuple[int, ...] = (200,)) -> None:
        if response.status_code not in ok:
            code = ""
            try:
                root = ElementTree.fromstring(response.content)
                node = next((n for n in root.iter() if _local_name(n.tag) == "Code"), None)
                code = (node.text or "")[:64] if node is not None else ""
            except ElementTree.ParseError:
                pass
            raise StorageError(f"{what}失败（HTTP {response.status_code} {code}）".strip())

    def put_file(self, path: str, local: Path, *, headers: dict[str, str] | None = None) -> None:
        if not valid_path(path):
            raise StorageError("文件路径不合规")
        digest = hashlib.sha256()
        size = 0
        with Path(local).open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        if size > 5 * 1024**3:
            raise StorageError("单个文件超过 5GB，要改成分块上传")

        def chunks() -> Iterator[bytes]:
            with Path(local).open("rb") as fh:
                while chunk := fh.read(_CHUNK):
                    yield chunk

        extra = {"content-length": str(size), **(headers or {})}
        response = self._request(
            "PUT", path, headers=extra, body=chunks(), payload_hash=digest.hexdigest()
        )
        self._check(response, "上传")

    def exists(self, path: str) -> bool:
        response = self._request("HEAD", path)
        if response.status_code == 404:
            return False
        self._check(response, "查文件")
        return True

    def delete(self, path: str) -> None:
        response = self._request("DELETE", path)
        self._check(response, "删除", ok=(200, 204, 404))

    def url(self, path: str, max_age: int) -> str:
        host, encoded = self._where(path)
        return presign_url(
            self.config,
            host,
            encoded,
            max(1, min(max_age, 7 * 86400)),
            datetime.now(UTC),
            scheme=self.scheme,
        )

    def list(self, prefix: str) -> list[StoredObject]:
        found: list[StoredObject] = []
        marker = ""
        for _ in range(1000):
            query = {"prefix": prefix, "max-keys": "1000"}
            if marker:
                query["marker"] = marker
            response = self._request("GET", "", query=query)
            self._check(response, "列文件")
            root = ElementTree.fromstring(response.content)
            truncated, next_marker, page = False, "", []
            for node in root:
                name = _local_name(node.tag)
                if name == "IsTruncated":
                    truncated = (node.text or "").strip().lower() == "true"
                elif name == "NextMarker":
                    next_marker = (node.text or "").strip()
                elif name == "Contents":
                    item = {_local_name(c.tag): (c.text or "") for c in node}
                    stamp = item.get("LastModified", "").replace("Z", "+00:00")
                    page.append(
                        StoredObject(
                            item.get("Key", ""),
                            datetime.fromisoformat(stamp) if stamp else datetime.now(UTC),
                            int(item.get("Size") or 0),
                        )
                    )
            found.extend(page)
            if not truncated:
                return found
            following = next_marker or (page[-1].key if page else "")
            if not following or following <= marker:
                raise StorageError("列文件翻页没有下一页标记")
            marker = following
        raise StorageError("文件太多，没有列完")


# ---- 按配置选后端 ----


def s3_config(settings: Settings) -> S3Config:
    return S3Config(
        endpoint=settings.s3_endpoint.rstrip("/"),
        region=settings.s3_region.strip(),
        bucket=settings.s3_bucket.strip(),
        access_key=settings.s3_access_key.get_secret_value().strip()
        if settings.s3_access_key
        else "",
        secret_key=settings.s3_secret_key.get_secret_value().strip()
        if settings.s3_secret_key
        else "",
        path_style=settings.s3_path_style,
    )


def backend(settings: Settings | None = None) -> Backend:
    settings = settings or get_settings()
    if settings.storage_backend == "disk":
        if settings.storage_dir is None:
            raise StorageError("没有配 YOUJU_STORAGE_DIR")
        return DiskBackend(settings.storage_dir)
    if settings.storage_backend == "s3":
        return S3Backend(s3_config(settings))
    raise StorageError("云托管对象存储不走这里")


def http_client(**kwargs: Any) -> httpx.Client:
    """读对象存储临时链接、调存储接口用的客户端（调用方负责关闭）。

    cloudbase、s3：httpx.Client(trust_env=False)，和原来一样；disk：另挂 DiskTransport。"""
    settings = get_settings()
    if settings.storage_backend != "disk":
        return httpx.Client(trust_env=False, **kwargs)
    return httpx.Client(
        trust_env=False, mounts={FAKE_ORIGIN: DiskTransport(backend(settings))}, **kwargs
    )


# ---- 按云托管存储接口的格式回（backup._open_api 在 disk、s3 时调这里） ----


def _paths(file_ids: list[Any]) -> list[tuple[Any, str | None]]:
    return [(fid, path_of(fid) if isinstance(fid, str) else None) for fid in file_ids]


def open_api(api: str, payload: dict) -> dict:
    """照 /tcb/uploadfile、/tcb/batchdownloadfile、/tcb/batchdeletefile 的返回格式做。
    出错抛 StorageError（调用方 backup._open_api 换成可重试的 BackupError）。"""
    store = backend()
    env = payload.get("env")
    if not isinstance(env, str) or not env:
        return {"errcode": -1, "errmsg": "缺少 env"}
    if api == "/tcb/uploadfile":
        path = payload.get("path")
        if not isinstance(path, str) or not valid_path(path):
            return {"errcode": 40097, "errmsg": "invalid path"}
        # 只给文件 ID（调用方拿它算前缀）；真上传走 upload()，不经过这里的地址
        return {
            "errcode": 0,
            "errmsg": "ok",
            "url": FAKE_ORIGIN + "/upload",
            "token": "-",
            "authorization": "-",
            "cos_file_id": "-",
            "file_id": file_id(env, path),
        }
    if api == "/tcb/batchdownloadfile":
        items = []
        for entry in payload.get("file_list") or []:
            fid = entry.get("fileid") if isinstance(entry, dict) else None
            max_age = entry.get("max_age", 600) if isinstance(entry, dict) else 600
            path = path_of(fid) if isinstance(fid, str) else None
            if path is None or not store.exists(path):
                items.append(
                    {
                        "fileid": fid,
                        "status": _MISSING,
                        "errmsg": "file not exist",
                        "download_url": "",
                    }
                )
                continue
            age = max_age if type(max_age) is int and max_age > 0 else 600
            items.append(
                {"fileid": fid, "status": 0, "errmsg": "ok", "download_url": store.url(path, age)}
            )
        return {"errcode": 0, "errmsg": "ok", "file_list": items}
    if api == "/tcb/batchdeletefile":
        items = []
        for fid, path in _paths(payload.get("fileid_list") or []):
            if path is None:
                items.append({"fileid": fid, "status": -1, "errmsg": "invalid fileid"})
                continue
            store.delete(path)
            items.append({"fileid": fid, "status": 0, "errmsg": "ok"})
        return {"errcode": 0, "errmsg": "ok", "delete_list": items}
    return {"errcode": -1, "errmsg": "不支持的存储接口"}


def upload(env: str, path: str, local: Path) -> str:
    """直接存到 disk、s3，返回文件 ID。"""
    backend().put_file(path, Path(local))
    return file_id(env, path)


def list_objects(prefix: str) -> list[StoredObject]:
    return backend().list(prefix)
