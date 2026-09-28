"""政府文件详情页解析；只保留可从页面直接确认的字段。"""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from youju.crawler.dates import parse_date_detail
from youju.crawler.urls import normalize_url

_FILE_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "wps", "et", "zip", "rar", "ofd"}
# 一篇通知收录的附件个数上限：正常通知不过几个到几十个，异常页面（整站文件目录等）不全收
MAX_ATTACHMENTS = 50
_FILE_PATTERN = re.compile(r"\.(pdf|docx?|xlsx?|wps|et|zip|rar|ofd)(?:$|[?#])", re.I)
_DOC_NUMBER = re.compile(
    r"[\u4e00-\u9fff]{1,16}(?:〔\s*20\d{2}\s*〕|\[\s*20\d{2}\s*\]"
    r"|（\s*20\d{2}\s*）|\(\s*20\d{2}\s*\)|【\s*20\d{2}\s*】)\s*\d{1,5}\s*号"
)
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]|\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
_BANK = re.compile(
    r"((?:银行卡|银行账号|银行帐号|账户|账号|帐号|卡号)\s*[:：]?\s*)(?:\d[ -]?){11,18}\d(?!\d)"
)
_EMAIL = re.compile(r"(?<![\w.])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_CONTACT = re.compile(r"(联系人\s*[:：]\s*)[\u4e00-\u9fff·]{2,8}")
_NAME_PHONE = re.compile(r"(?:[\u4e00-\u9fff·]\s*){2,4}(?=0\d{2,3}[-－]\d{7,8})")
_NAME_MASKED_PHONE = re.compile(r"(?:[\u4e00-\u9fff·]\s*){2,4}(?=\[电话已脱敏\])")
_PHONE = re.compile(r"(?<!\d)0\d{2,3}[-－]\d{7,8}(?!\d)")
_AGENCY = re.compile(
    r"^[\u4e00-\u9fff（）()、\s]{3,100}(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]{1,20}）)?$"
)
_AGENCY_END = re.compile(
    r"(?:厅|局|委|委员会|部|中心|政府|办公室|研究院|院|署|海关|分行)(?:（[^）]+）)?$"
)
_BRANCH_AGENCIES = (
    ("国家税务总局", "税务局"),
    ("国家金融监督管理总局", "监管局"),
    ("中国人民银行", "分行"),
    ("国家外汇管理局", "分局"),
    ("海关总署", "海关"),
)
_DATE = re.compile(r"20\d{2}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*日?")
_JUNK_CLASS = re.compile(
    r"(?:share|related|footer|breadcrumb|toolbar|copyright|qrcode|qr-code|print|pagination)", re.I
)
_BODY_SELECTORS = (
    '//*[@id="zoom"]',
    '//*[@id="UCAP-CONTENT" or @id="contentText"]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " TRS_Editor ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article_content ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " html-content-fun ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " main-txt ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]',
    "//article",
    '//*[contains(concat(" ", normalize-space(@class), " "), " pages_content ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " article_area ")]',
    '//*[contains(concat(" ", normalize-space(@class), " "), " content ")]',
)
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4", "br"}


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str
    extension: str


@dataclass
class DetailResult:
    body_text: str = ""
    issuer: str | None = None
    doc_number: str | None = None
    publish_date: date | None = None
    attachments: list[Attachment] = field(default_factory=list)
    failure_reason: str | None = None


def redact_sensitive(text: str) -> str:
    """入库和测试快照共用的脱敏规则；不更改原站链接。"""
    text = _ID_CARD.sub("[身份证号已脱敏]", text)
    text = _MOBILE.sub("[手机号已脱敏]", text)
    text = _BANK.sub(r"\1[银行账号已脱敏]", text)
    text = _CONTACT.sub(r"\1[姓名已脱敏]", text)
    text = _NAME_PHONE.sub("[姓名已脱敏]", text)
    text = _NAME_MASKED_PHONE.sub("[姓名已脱敏]", text)
    text = _EMAIL.sub("[邮箱已脱敏]", text)
    return _PHONE.sub("[电话已脱敏]", text)


def _plain_lines(node: etree._Element) -> list[str]:
    parts: list[str] = []

    def visit(element: etree._Element) -> None:
        if not isinstance(element.tag, str):
            return
        if element.tag.lower() in {"script", "style", "noscript", "nav", "footer"}:
            return
        marker = f"{element.get('class', '')} {element.get('id', '')}"
        if _JUNK_CLASS.search(marker):
            return
        if element.tag.lower() == "br":
            parts.append("\n")
        if element.text:
            parts.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                parts.append(child.tail)
        if element.tag.lower() in _BLOCK_TAGS:
            parts.append("\n")

    visit(node)
    return [line for raw in "".join(parts).splitlines() if (line := " ".join(raw.split()))]


def _body_node(root: etree._Element) -> etree._Element | None:
    for selector in _BODY_SELECTORS:
        nodes = root.xpath(selector)
        candidates = [n for n in nodes if len(" ".join(_plain_lines(n))) >= 20]
        if candidates:
            return max(candidates, key=lambda n: len(" ".join(_plain_lines(n))))
    return _density_body_node(root)


def _density_body_node(root: etree._Element) -> etree._Element | None:
    """站点没有常见正文容器时，按非链接文字量和段落密度选择内容块。"""
    candidates: list[tuple[float, int, etree._Element]] = []
    for node in root.xpath("//main|//article|//section|//div"):
        if _JUNK_CLASS.search(f"{node.get('class', '')} {node.get('id', '')}"):
            continue
        lines = _plain_lines(node)
        text = " ".join(lines)
        if len(text) < 40:
            continue
        link_chars = sum(len(" ".join(a.itertext())) for a in node.xpath(".//a"))
        if link_chars / max(len(text), 1) > 0.35:
            continue
        paragraphs = sum(1 for line in lines if len(line) >= 20)
        if paragraphs < 2 and len(text) < 100:
            continue
        depth = len(node.xpath("ancestor::*"))
        score = len(text) - 2 * link_chars + min(paragraphs, 20) * 12
        candidates.append((score, depth, node))
    if not candidates:
        return None
    best = max(score for score, _, _ in candidates)
    return max((c for c in candidates if c[0] >= best * 0.85), key=lambda c: c[1])[2]


def _metadata(root: etree._Element, name: str) -> str | None:
    for node in root.xpath("//meta[@name or @property]"):
        key = (node.get("name") or node.get("property") or "").lower()
        if key == name.lower():
            return (node.get("content") or "").strip() or None
    return None


def _issuer(lines: list[str], root: etree._Element, prefix: str) -> str | None:
    # 落款比站点的 ContentSource 更具体，多部门联合发文也应完整保留。
    for i in range(len(lines) - 1, max(-1, len(lines) - 15), -1):
        if not _DATE.fullmatch(lines[i]):
            continue
        agencies: list[str] = []
        for line in reversed(lines[max(0, i - 8) : i]):
            if _AGENCY.fullmatch(line) and len(line) <= 60:
                agencies.append(_split_issuer_line(line))
            elif agencies:
                break
        if agencies:
            return "、".join(reversed(agencies))
    for match in re.finditer(
        r"(?:发文机关|发文单位|发布机构|制发机关)\s*[:：]\s*([^\n]{2,80})", prefix
    ):
        value = match.group(1).strip()
        if _AGENCY_END.search(value):
            return value
    for name in ("issuer", "publishingorganization", "contentSource"):
        value = _metadata(root, name)
        if value and len(value) <= 60 and _AGENCY_END.search(value):
            return value
    return None


def _split_issuer_line(line: str) -> str:
    """落款中的空格可分隔联合发文机关，也可只是总局与地方分支间的排版空格。"""
    for parent, branch in _BRANCH_AGENCIES:
        line = re.sub(
            rf"({parent})\s+(?=[\u4e00-\u9fff]+{branch}(?:\s|、|$))",
            r"\1",
            line,
        )
    return re.sub(r"(?<=[厅局部委署关行])\s+(?=[\u4e00-\u9fff])", "、", line)


def _doc_number(lines: list[str], root: etree._Element, prefix: str) -> str | None:
    for name in ("documentNumber", "docNumber", "fileno"):
        value = _metadata(root, name)
        if value and (match := _DOC_NUMBER.search(value)):
            return "".join(match.group().split())
    for line in prefix.splitlines():
        if len(line) <= 100 and (match := _DOC_NUMBER.search(line)):
            return "".join(match.group().split())
    for line in lines[:3]:
        if (match := _DOC_NUMBER.fullmatch(line)) or (
            re.match(r"^(?:文号|发文字号|文件编号)\s*[:：]", line)
            and (match := _DOC_NUMBER.search(line))
        ):
            return "".join(match.group().split())
    return None


def _publish_date(root: etree._Element, prefix: str) -> date | None:
    for name in ("PubDate", "publishDate", "published_time", "article:published_time"):
        value = _metadata(root, name)
        if value:
            parsed, explicit_year = parse_date_detail(value)
            if explicit_year:
                return parsed
    match = re.search(
        r"(?:发布日期|发布时间|发布于|发稿时间|日期)\s*[:：]?\s*(20\d{2}[^\n]{0,20})",
        prefix,
    )
    if match:
        parsed, explicit_year = parse_date_detail(match.group(1))
        if explicit_year:
            return parsed
    return None


def _attachments(root: etree._Element, url: str) -> list[Attachment]:
    result: list[Attachment] = []
    seen: set[str] = set()
    page_host = urlsplit(url).hostname
    for anchor in root.xpath("//a[@href]"):
        target = normalize_url(url, anchor.get("href"))
        if not target or target in seen:
            continue
        label = " ".join(anchor.text_content().split())
        filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
        match = _FILE_PATTERN.search(target) or _FILE_PATTERN.search(label)
        if not match or match.group(1).lower() not in _FILE_EXTENSIONS:
            continue
        # 外站 PDF 往往是正文引用。只有明确标作附件时才收入附件题录。
        if urlsplit(target).hostname != page_host and "附件" not in label:
            continue
        if redact_sensitive(target) != target:
            continue
        name = label or filename
        seen.add(target)
        result.append(Attachment(redact_sensitive(name), target, match.group(1).lower()))
        if len(result) >= MAX_ATTACHMENTS:
            break
    return result


def parse_detail(html_text: str, url: str) -> DetailResult:
    """解析公开 HTML；防护页返回失败原因，字段不猜测。"""
    if not html_text.strip():
        return DetailResult(failure_reason="页面内容为空")
    try:
        root = html.fromstring(html_text)
    except (etree.ParserError, ValueError) as exc:
        return DetailResult(failure_reason=f"HTML 无法解析：{exc.__class__.__name__}")
    title = " ".join(root.xpath("//title/text()"))[:200]
    body = root.xpath("//body")
    visible = "\n".join(_plain_lines(body[0])) if body else ""
    if re.search(
        r"验证码|人机验证|安全验证|访问过于频繁|异常访问|请开启.{0,12}JavaScript",
        title + visible[:500],
        re.I,
    ):
        return DetailResult(failure_reason="防护页或验证码")
    node = _body_node(root)
    if node is None:
        attachments = _attachments(root, url)
        return DetailResult(
            issuer=_issuer([], root, visible[:700]),
            doc_number=_doc_number([], root, visible[:700]),
            publish_date=_publish_date(root, visible[:700]),
            attachments=attachments,
            failure_reason=None if attachments else "未识别到正文容器",
        )
    lines = _plain_lines(node)
    raw_body = "\n".join(lines)
    prefix = (
        visible.split(lines[0], 1)[0][-700:] if lines and lines[0] in visible else visible[:700]
    )
    attachments = _attachments(root, url)
    # PDF-only 页面只有下载入口，HTML 正文为空；仍保留附件题录。
    if len(raw_body) < 30 or ("点击正文" in raw_body and not any(len(line) > 60 for line in lines)):
        raw_body = ""
    return DetailResult(
        body_text=redact_sensitive(raw_body),
        issuer=_issuer(lines, root, prefix),
        doc_number=_doc_number(lines, root, prefix),
        publish_date=_publish_date(root, prefix),
        attachments=attachments,
        failure_reason=None if raw_body or attachments else "未识别到正文或附件",
    )
