"""用 AI 辅助文档类型判断，并复用现有分类口径。"""

from dataclasses import dataclass

from .client import ChatClient


@dataclass(frozen=True)
class AIClassification:
    kind: str
    confidence: float
    reason: str


_SYSTEM = """你是有据后台的文档分类助手。必须只返回 JSON 对象。
类别只能是：申报通知、名单、政策、其他。
申报通知：组织企业申报、征集、推荐、遴选、评选、报名、申请、申领、揭榜或服务券；职称申报通知仍算申报通知。
名单：企业或机构的名单、公示、拟认定/入库/资助/立项/评审/验收等结果；招聘、职称评审、考试合格人员、专家、采购、执法检查、行政处罚等个人或非企业名单不算名单。
政策：办法、规定、意见、方案、细则、条例、规划、措施等政策文件，含印发或征求意见的具名文件。
其他：不符合上述规则的内容，拿不准时归其他。
返回字段 kind、confidence（0 到 1 的数字）、reason（不超过 60 字）。"""


def classify_with_ai(client: ChatClient, title: str, text: str) -> AIClassification | None:
    """分类标题和正文；无效类别或字段时丢弃结果。"""
    try:
        result = client.complete_json(_SYSTEM, f"标题：{title}\n正文：{text[:3000]}")
        kind = result.get("kind")
        if kind not in ("申报通知", "名单", "政策", "其他"):
            return None
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
        reason = str(result.get("reason", ""))[:60]
        return AIClassification(kind, confidence, reason)
    except (TypeError, ValueError, KeyError):
        return None
