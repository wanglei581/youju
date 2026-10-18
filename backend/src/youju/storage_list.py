"""云托管对象存储按前缀列文件（批量工具结果文件巡检用，见 youju/advisor_orphans.py）。

- 开放接口服务的存储接口只有上传（/tcb/uploadfile）、取下载链接（/tcb/batchdownloadfile）、
  删除（/tcb/batchdeletefile）三个，不能列文件。官方文档另给了一条路：对象存储底层是腾讯云 COS，
  用开放接口 http://api.weixin.qq.com/_/cos/getauth 取临时密钥，再"使用 COS SDK 操作存储桶，
  可以进行文件列表读取、文件下载、文件删除等操作"（微信云托管文档《API 和组件使用》
  《COS-SDK 服务端使用》，2026-10-18 核对）。这里只用它列文件（COS 的 GET Bucket），
  上传、删除照旧走开放接口。
- 不另装 COS SDK：签名按腾讯云 COS 文档《请求签名》自己算（HMAC-SHA1，签 host 和全部查询参数），
  测试里用文档的两个签名示例核对。
- 临时密钥只在内存里用：不打日志、不进报错信息。
- 存储桶名取自文件 ID（cloud://环境.存储桶/路径）：和 backup-verify 一样，用专门的路径
  backups/prefix-probe 申请一次上传链接，只取返回的文件 ID，不上传。地域用云托管默认的上海
  （官方《存储配置》："对象存储地域：默认上海地域"）；不是上海时列文件会报错，不会列错地方。
"""

import hashlib
import hmac
import re
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

from youju.backup import _BUSY_CODES, OPEN_API, PROBE_PATH, BackupError, _open_api, _retrying

REGION = "ap-shanghai"
# 每页最多 1000 个（COS 的上限）；超过 1000 页（100 万个文件）就停，不当成列完了
PAGE_SIZE = 1000
MAX_PAGES = 1000
# 签名有效时间：往前留 60 秒容忍两边时钟差（COS 的 SDK 也这样），往后 10 分钟够列一页
_SIGN_BEFORE = 60
_SIGN_AFTER = 600
_PREFIX = re.compile(r"cloud://([^./]+)\.([0-9a-z][0-9a-z-]{2,80})/")
_ERROR_CODE = re.compile(r"[A-Za-z]{1,64}")


@dataclass(frozen=True)
class Credentials:
    """临时密钥。repr 不显示内容，免得被打进日志。"""

    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True)
class StoredObject:
    key: str
    last_modified: datetime


def credentials(
    client: httpx.Client, *, sleep: Callable[[float], None] = clock.sleep
) -> Credentials:
    """经开放接口服务取临时密钥（免 access_token）。网络中断、服务繁忙时重试两次。"""

    def fetch() -> Credentials:
        api = "/_/cos/getauth"
        try:
            res = client.get(f"{OPEN_API}{api}", timeout=20)
        except httpx.TransportError as exc:
            raise BackupError(f"调用 {api} 网络失败", retryable=True) from exc
        if res.status_code >= 500:
            raise BackupError(f"调用 {api} 失败（HTTP {res.status_code}）", retryable=True)
        try:
            res.raise_for_status()
            data = res.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BackupError(f"调用 {api} 失败：{exc.__class__.__name__}") from exc
        if not isinstance(data, dict):
            raise BackupError(f"调用 {api} 失败：返回的不是密钥")
        values = [data.get(name) for name in ("TmpSecretId", "TmpSecretKey", "Token")]
        if not all(isinstance(value, str) and value for value in values):
            code = data.get("errcode")
            if type(code) is int:
                raise BackupError(
                    f"调用 {api} 失败（errcode {code}）", retryable=code in _BUSY_CODES
                )
            raise BackupError(f"调用 {api} 失败：返回里没有临时密钥")
        return Credentials(*values)

    return _retrying(fetch, sleep)


def bucket_of(
    client: httpx.Client, env: str, *, sleep: Callable[[float], None] = clock.sleep
) -> tuple[str, str]:
    """返回（文件 ID 前缀 cloud://环境.存储桶/，存储桶名）。"""
    link = _retrying(
        lambda: _open_api(client, "/tcb/uploadfile", {"env": env, "path": PROBE_PATH}), sleep
    )
    file_id = link.get("file_id")
    prefix = ""
    if isinstance(file_id, str) and file_id.endswith("/" + PROBE_PATH):
        prefix = file_id[: -len(PROBE_PATH)]
    match = _PREFIX.fullmatch(prefix)
    if not match or match.group(1) != env:
        raise BackupError("文件 ID 的格式和预期不同，没有列文件")
    return prefix, match.group(2)


def _encode(value: str) -> str:
    """签名用的 UrlEncode：字母、数字和 -_.~ 以外都编码（空格是 %20，/ 是 %2F）。"""
    return quote(value, safe="-_.~")


def _pairs(values: dict[str, str]) -> tuple[str, str]:
    """（键列表 a;b，键值串 a=1&b=2）：键编码后转小写，值编码，按键排序。"""
    items = sorted((_encode(key).lower(), _encode(value)) for key, value in values.items())
    return ";".join(key for key, _ in items), "&".join(f"{key}={value}" for key, value in items)


def http_string(method: str, path: str, params: dict[str, str], headers: dict[str, str]) -> str:
    """文档里的 HttpString：方法（小写）、路径（不编码）、查询参数、头部，各占一行。"""
    return f"{method.lower()}\n{path}\n{_pairs(params)[1]}\n{_pairs(headers)[1]}\n"


def _hmac_sha1(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha1).hexdigest()


def signature(sign_key: str, key_time: str, http: str) -> str:
    """StringToSign = sha1、KeyTime、SHA1(HttpString) 各占一行，再用 SignKey（十六进制字符串
    本身，不是解码后的字节）做 HMAC-SHA1。"""
    digest = hashlib.sha1(http.encode()).hexdigest()
    return _hmac_sha1(sign_key, f"sha1\n{key_time}\n{digest}\n")


def authorization(
    creds: Credentials,
    method: str,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    key_time: str,
) -> str:
    sign_key = _hmac_sha1(creds.secret_key, key_time)
    sign = signature(sign_key, key_time, http_string(method, path, params, headers))
    return "&".join(
        (
            "q-sign-algorithm=sha1",
            f"q-ak={creds.secret_id}",
            f"q-sign-time={key_time}",
            f"q-key-time={key_time}",
            f"q-header-list={_pairs(headers)[0]}",
            f"q-url-param-list={_pairs(params)[0]}",
            f"q-signature={sign}",
        )
    )


def _local(tag: str) -> str:
    """去掉 XML 命名空间（文档的返回示例没有，有也能认）。"""
    return tag.rsplit("}", 1)[-1]


def _text(node: ElementTree.Element, name: str) -> str | None:
    child = next((item for item in node if _local(item.tag) == name), None)
    return None if child is None else (child.text or "")


def _error_code(content: bytes) -> str:
    """COS 出错时返回的错误码（如 AccessDenied）；只取字母，不带出错误信息里的其他内容。"""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return ""
    code = (_text(root, "Code") or "").strip()
    return code if _ERROR_CODE.fullmatch(code) else ""


def _parse(content: bytes) -> tuple[str | None, bool, str | None, list[StoredObject]]:
    """（存储桶名，是否还有下一页，下一页标记，这一页的对象）。"""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise BackupError("文件列表的格式不对") from exc
    if _local(root.tag) != "ListBucketResult":
        raise BackupError("文件列表的格式不对")
    objects = []
    for item in root:
        if _local(item.tag) != "Contents":
            continue
        key = _text(item, "Key")
        try:
            modified = datetime.fromisoformat(_text(item, "LastModified") or "")
        except ValueError as exc:
            raise BackupError("文件列表里有读不出的修改时间") from exc
        if not key or modified.tzinfo is None:
            raise BackupError("文件列表里有读不出的文件名或修改时间")
        objects.append(StoredObject(key, modified))
    truncated = (_text(root, "IsTruncated") or "").strip().lower()
    if truncated not in ("true", "false"):
        raise BackupError("文件列表的格式不对：没有写是否列完")
    return _text(root, "Name"), truncated == "true", _text(root, "NextMarker"), objects


def _page(
    client: httpx.Client,
    creds: Credentials,
    host: str,
    params: dict[str, str],
    now: Callable[[], float],
) -> tuple[str | None, bool, str | None, list[StoredObject]]:
    start = int(now()) - _SIGN_BEFORE
    key_time = f"{start};{start + _SIGN_BEFORE + _SIGN_AFTER}"
    query = "&".join(f"{_encode(key)}={_encode(value)}" for key, value in sorted(params.items()))
    headers = {
        "Authorization": authorization(creds, "get", "/", params, {"host": host}, key_time),
        "x-cos-security-token": creds.token,
    }
    try:
        res = client.get(f"https://{host}/?{query}", headers=headers, timeout=30)
    except httpx.TransportError as exc:
        raise BackupError("列对象存储文件网络失败", retryable=True) from exc
    if res.status_code != 200:
        code = _error_code(res.content)
        raise BackupError(
            f"列对象存储文件失败（HTTP {res.status_code}{'，' + code if code else ''}）",
            retryable=res.status_code >= 500,
        )
    return _parse(res.content)


def list_objects(
    client: httpx.Client,
    creds: Credentials,
    bucket: str,
    prefix: str,
    *,
    sleep: Callable[[float], None] = clock.sleep,
    now: Callable[[], float] = clock.time,
) -> list[StoredObject]:
    """列出存储桶里以 prefix 开头的全部文件（按 NextMarker 翻页；每页网络中断、服务繁忙时
    重试两次）。任何一页不对就整个报错，不返回列了一半的结果。"""
    host = f"{bucket}.cos.{REGION}.myqcloud.com"
    found: list[StoredObject] = []
    marker = ""
    for _ in range(MAX_PAGES):
        params = {"prefix": prefix, "max-keys": str(PAGE_SIZE)}
        if marker:
            params["marker"] = marker
        name, truncated, next_marker, objects = _retrying(
            lambda params=params: _page(client, creds, host, params, now), sleep
        )
        if name != bucket:
            raise BackupError("列出来的存储桶和文件 ID 里的不一致，停止")
        if any(not item.key.startswith(prefix) for item in objects):
            raise BackupError("列出来的文件不在要列的路径下，停止")
        found.extend(objects)
        if not truncated:
            return found
        following = next_marker or (objects[-1].key if objects else "")
        if not following or following <= marker:
            raise BackupError("文件列表翻页没有下一页标记，停止")
        marker = following
    raise BackupError(f"文件超过 {MAX_PAGES} 页，没有列完，停止")
