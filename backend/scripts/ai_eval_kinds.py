"""用人工标注的 489 条标题衡量 AI 类型判断的正确率，和关键词规则对比（验收 A5）。

需要配置 YOUJU_AI_API_KEY（backend/.env 或环境变量）。只发标题，不发任何用户数据。
评测集的前四批参与过调规则，规则在上面的正确率偏高；对新数据的正确率另见 A5 抽样记录。

用法（在 backend/ 目录下）：
    uv run python scripts/ai_eval_kinds.py            # 全部 489 条
    uv run python scripts/ai_eval_kinds.py --limit 50 # 先试 50 条
"""

import argparse
import json
from pathlib import Path

from youju.ai import AIError, get_chat_client
from youju.ai.classify import classify_with_ai
from youju.classify import doc_kind

LABELED = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "classify_labeled.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    client = get_chat_client()
    if client is None:
        print("没有配置 YOUJU_AI_API_KEY，无法评测")
        return 1
    items = json.loads(LABELED.read_text(encoding="utf-8"))[: args.limit]
    ai_right = rule_right = both_agree = both_agree_right = failed = 0
    prompt_tokens = completion_tokens = 0
    wrong: list[tuple[str, str, list[str]]] = []
    with client:
        for item in items:
            title, expected = item["title"], item["expected"]
            rule = doc_kind(title)
            rule_right += rule in expected
            try:
                result = classify_with_ai(client, title, "")
            except AIError:
                result = None
            prompt_tokens += client.last_usage["prompt_tokens"]
            completion_tokens += client.last_usage["completion_tokens"]
            if result is None:
                failed += 1
                continue
            ai_right += result.kind in expected
            if result.kind == rule:
                both_agree += 1
                both_agree_right += rule in expected
            if result.kind not in expected:
                wrong.append((title, result.kind, expected))
    n = len(items)
    print(f"共 {n} 条（AI 调用失败或结果无效 {failed} 条，按判错算）")
    print(f"关键词规则正确率：{rule_right / n:.1%}")
    print(f"AI 正确率：{ai_right / n:.1%}（模型 {client.model}）")
    if both_agree:
        print(
            f"AI 和规则一致的 {both_agree} 条（{both_agree / n:.0%}），其中正确 "
            f"{both_agree_right / both_agree:.1%}；不一致的 {n - both_agree} 条交给人定"
        )
    print(f"用量：输入 {prompt_tokens}、输出 {completion_tokens} tokens")
    for title, got, expected in wrong[:30]:
        print(f"  AI 判错：{title[:50]}｜AI={got}｜应为={'/'.join(expected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
