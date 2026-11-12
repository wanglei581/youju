"""盘点用的页面缓存：抓过的页面存在本机，重跑时直接用，不重复打扰政府网站。

每个网址一个 gzip 压缩的 JSON 文件（含抓取结果和网页文字）。失败的结果也存：网站暂时打不开时
重跑不会一遍遍去敲门；超过有效期（默认成功 14 天、失败 2 天）才重新抓。
"""

import gzip
import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

from youju.crawler.fetcher import FetchResult, PoliteFetcher

_TEXT_MAX = 3_000_000  # 只存前 300 万字：导航和列表页够用了
REFUSED_DAYS = 30
_REFUSED_HTTP = {401, 403, 412, 521}


def _refused(cached: dict) -> bool:
    return cached.get("status") == "robots_disallowed" or (
        cached.get("status") == "http_error" and cached.get("http_status") in _REFUSED_HTTP
    )


class PageCache:
    def __init__(
        self,
        root: Path,
        fetcher: PoliteFetcher | None,
        ok_days: float = 14,
        fail_days: float = 2,
        offline: bool = False,
    ) -> None:
        self.root = root
        self.fetcher = fetcher
        self.ok_seconds = ok_days * 86400
        self.fail_seconds = fail_days * 86400
        # offline：只读缓存，不联网（重新生成清单、跑测试时用）
        self.offline = offline
        self._lock = threading.Lock()
        self._url_locks: dict[str, threading.Lock] = {}
        self.hits = 0
        self.fetches = 0

    def _path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.root / "pages" / h[:2] / f"{h}.json.gz"

    def get(self, url: str, same_address_only: bool = False) -> FetchResult:
        """same_address_only：只取这个地址本身、跳到别处不跟（读列表接口时用，
        见 PoliteFetcher.fetch）。"""
        # 同一网址同时只抓一次：几个部门共用一个网站时，后来的直接用缓存
        with self._lock:
            lock = self._url_locks.setdefault(url, threading.Lock())
        with lock:
            return self._get(url, same_address_only)

    def _get(self, url: str, same_address_only: bool = False) -> FetchResult:
        path = self._path(url)
        cached = self._read(path)
        if cached is not None:
            age = time.time() - cached.pop("_saved_at", 0)
            if cached.get("status") == "ok":
                limit = self.ok_seconds
            elif _refused(cached):
                # 网站明确拒绝过（robots 禁止、401/403/412/521）：30 天内不再去请求，
                # 重抓失败页面（--retry-failed）也不碰它们，免得像在反复敲门
                limit = REFUSED_DAYS * 86400
            else:
                limit = self.fail_seconds
            if self.offline or age < limit:
                with self._lock:
                    self.hits += 1
                return FetchResult(**cached)
        if self.offline or self.fetcher is None:
            return FetchResult(url, "not_cached", error="缓存里没有，离线模式不抓")
        result = (
            self.fetcher.fetch(url, same_address_only=True)
            if same_address_only
            else self.fetcher.fetch(url)
        )
        if result.status == "ok" and result.content.startswith(b"%PDF-"):
            # 年度报表有的只发 PDF：存它的文字（表里有"首页网址"）
            from youju.crawler.pdf import extract_pdf_text

            result.text = extract_pdf_text(result.content)[0]
        with self._lock:
            self.fetches += 1
        self._write(path, result)
        return result

    def _read(self, path: Path) -> dict | None:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, result: FetchResult) -> None:
        data = asdict(result)
        data.pop("content", None)  # 原始字节不存，文字够用
        data["text"] = (data.get("text") or "")[:_TEXT_MAX]
        data["_saved_at"] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{threading.get_ident()}")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        tmp.replace(path)
