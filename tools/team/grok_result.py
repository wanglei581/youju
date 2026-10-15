"""只认 Grok 最后的结构化完成事件；普通日志和时间戳不是调用状态。"""

import json
import re
import sys
from pathlib import Path


def parse_result(folder: Path, process_code: int) -> int:
    result = None
    for line in (folder / "stream.jsonl").read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    body = result.get("result") if result else None
    success = bool(
        process_code == 0
        and result
        and result.get("subtype") == "success"
        and result.get("is_error") is False
        and isinstance(body, str)
        and body.strip()
    )
    # 失败或被中断时清掉旧稿，不能把上一次的 out.md 当这一次的审阅。
    (folder / "out.md").write_text(body if success else "", encoding="utf-8")
    status = {"ok": success, "process_exit": process_code,
              "subtype": result.get("subtype") if result else None,
              "is_error": result.get("is_error") if result else None}
    (folder / "status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    if success:
        print("exit ok (grok)")
        print(body)
        return 0
    # 只在失败时识别明确的 HTTP/额度错误；不扫描生成正文，时间戳中的 402 不算错误。
    logs = (folder / "log.txt").read_text(errors="replace")
    quota = re.search(r"(?:HTTP(?:/[\d.]+)?\s+402\b|(?:status(?:_code)?|code)\s*[:=]\s*402\b|402\s+Payment Required|usage balance (?:is )?(?:insufficient|exhausted)|insufficient (?:usage )?balance)", logs, re.I)
    print("grok 额度不足（明确的 HTTP/额度错误）" if quota else "grok 未正常完成；查看 status.json 和本机日志")
    return 2 if quota else 1


if __name__ == "__main__":
    sys.exit(parse_result(Path(sys.argv[1]), int(sys.argv[2])))
