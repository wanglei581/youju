"""交给模型的文字（D-114）：所有把库里正文、附件文字交给模型的地方都调这一个函数，不各写各的。

协议和隐私保护指引写的是"发送前隐去其中的身份证号、手机号、电话、邮箱、银行账号和联系人姓名"：
入库规则（redact_sensitive）再遮一遍——旧规则漏遮的正文还在库里，不改写库里存的——再按页面那套
规则补遮号码、联系人姓名和邮箱（v1.model_body_text），标记写成入库的写法（"[电话已脱敏]"这类）。

调用方照旧拿库里存的原文核对：写结果之前正文换没换、模型给的原句和证据在正文里的位置，都对库里
存的原文（申报条件对 conditions.public_body，页面上用的也是它）；带"已脱敏]"的原句对不上原文，
照旧不用。
"""

from youju.crawler.detail import redact_sensitive


def model_text(raw: str | None) -> str:
    """库里存的一段文字交给模型之前的写法（见模块说明）。处理两次结果一样。"""
    # 延迟引入 v1，避免 API 路由装载时形成循环依赖（同 content._check）
    from youju.api import v1

    return v1.model_body_text(redact_sensitive(raw or ""))
