"""在本机生成对象存储 attachments/ 的清单，给容器里的 youju settle-missing-copies 用。

只读：调用 wxcloud storage:list（本机已登录的微信云托管 CLI）列出对象，不下载、不删除。
输出第一行 JSON（v、bucket、prefix、listed_at、count、sha256），后面是 base64：每个对象内容
SHA-256 的前 4 字节，从小到大排好接在一起（每行 76 个字符，便于粘贴进容器终端）。

用法：python3 scripts/attachment_listing.py <环境 ID> > listing.txt
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

PREFIX = "attachments/"
KEY = re.compile(r"attachments/([0-9a-f]{64})\.(?:docx?|xlsx?|pptx?|pdf)")
HEAD_BYTES = 4


def _page(env: str, marker: str | None) -> dict:
    cmd = ["wxcloud", "storage:list", PREFIX, "-e", env, "-m", "storage", "-j"]
    if marker:
        cmd += ["--marker", marker]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=180).stdout
    # CLI 先打印两行说明再打印 JSON
    return json.loads(out[out.index("{") :])


def listing(env: str) -> tuple[str, list[bytes], int]:
    bucket, heads, others, marker = None, set(), 0, None
    while True:
        page = _page(env, marker)
        name = page.get("Name")
        if not isinstance(name, str) or (bucket is not None and name != bucket):
            raise SystemExit("清单里的存储桶名不一致，停止")
        bucket = name
        contents = page.get("Contents") or []
        for item in contents:
            match = KEY.fullmatch(item.get("Key", ""))
            if match:
                heads.add(bytes.fromhex(match.group(1)[: HEAD_BYTES * 2]))
            else:
                others += 1
        if str(page.get("IsTruncated")).lower() != "true":
            break
        marker = page.get("NextMarker") or (contents[-1]["Key"] if contents else None)
        if not marker:
            raise SystemExit("清单分页没有下一页标记，停止")
    if bucket is None or env not in bucket:
        raise SystemExit("存储桶名和环境 ID 对不上，停止")
    return bucket, sorted(heads), others


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    bucket, heads, others = listing(sys.argv[1])
    if not heads:
        raise SystemExit("attachments/ 下一个副本都没有，不生成清单")
    payload = b"".join(heads)
    head = {
        "v": 1,
        "bucket": bucket,
        "prefix": PREFIX,
        # 本机自带的 python3 可能是 3.9（没有 datetime.UTC）
        "listed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),  # noqa: UP017
        "count": len(heads),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    print(json.dumps(head, separators=(",", ":")))
    text = base64.b64encode(payload).decode()
    for i in range(0, len(text), 76):
        print(text[i : i + 76])
    print(f"对象 {len(heads)} 个（另有 {others} 个不是附件副本的文件，未列入）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
