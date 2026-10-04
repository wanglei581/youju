"""数据质量抽检表（收费门槛 v1 第一节“数据质量”）：抽样并先做程序能做的核对，其余交给人看。

两个函数都只读，不写任何表：

- ``sample_notices``：从小程序能看到的申报通知里按来源分层轮流抽样，列出对外展示的截止日期、
  已核对字段和各自的证据原句，程序核对来源可追溯、证据原句是否逐字出现在正文里、
  证据原句里有没有这个日期。
- ``sample_compare``：一半真实企业名、一半“近似但不同”的名称，分别跑名单比对的查找核心
  （``lookup_list_matches``，与 ``compare_lists`` 同样不设扫描和结果上限），程序标出要人工看的行。

同一个 seed、同一份数据得到同一份抽检表。
"""

from __future__ import annotations

import csv
import io
import random
import re
import unicodedata
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from youju.api.v1 import _approved, _approved_fields, _base, _details, _kind_is
from youju.dedup import canonical_ids, groups_for
from youju.lists.lookup import lookup_list_matches, public_condition
from youju.lists.rules import is_person_query, normalize_credit_code, normalize_name
from youju.models import Document, DocumentDetail, ListEntry, Source

PASS, FAIL, UNKNOWN, NOT_APPLICABLE = "通过", "不通过", "无法判断", "不适用"

NOTICE_HEADERS = (
    "序号",
    "文件编号",
    "标题",
    "来源名称",
    "发文机关",
    "文号",
    "发布日期",
    "原文网址",
    "截止日期（已核对）",
    "截止证据原句",
    "已核对字段",
    "字段证据原句",
    "正文",
    "核对①来源可追溯",
    "核对②截止证据",
    "核对③字段证据",
    "程序核对",
    "人工结论",
    "备注",
)
COMPARE_HEADERS = (
    "序号",
    "输入名称",
    "是否真实名",
    "近似名生成方式",
    "近似名依据（真实名称）",
    "比对结果",
    "命中名单",
    "命中的名称",
    "程序判断",
    "人工结论",
    "备注",
)

# ---------------------------------------------------------------- 日期和证据


_CN_DATE = re.compile(r"(?<!\d)(?:(?P<y>\d{4})\s*年\s*)?(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})(?!\d)")
# 有年份时两个分隔符必须相同（2026-10-30、2026/10/30、2026.10.30）；没有年份时如 10.30、10/30。
# 前面不能紧挨数字或小数点，后面不能紧挨数字，避免把 110.30、10.301 当作日期。
_NUM_DATE = re.compile(
    r"(?<![\d.])(?:(?P<y>\d{4})\s*(?P<s>[-./])\s*)?(?P<m>\d{1,2})\s*(?(s)(?P=s)|[-./])\s*"
    r"(?P<d>\d{1,2})(?![\d])"
)


def date_in_text(day: date, text: str) -> bool:
    """文字里有没有写到这一天：2026年10月30日、2026-10-30、2026/10/30、2026.10.30、10月30日、
    10.30 等写法，月和日可以带前导零、全角数字或中间有空格。写了年份的，年份也要对上。"""
    value = unicodedata.normalize("NFKC", text or "")
    for pattern in (_CN_DATE, _NUM_DATE):
        for match in pattern.finditer(value):
            year = match.group("y")
            if (
                int(match.group("m")) == day.month
                and int(match.group("d")) == day.day
                and (year is None or int(year) == day.year)
            ):
                return True
    return False


def collapse_spaces(text: str | None) -> str:
    """只把连续空白（含换行、全角空格）合并成一个空格，其他字符不动。"""
    return " ".join((text or "").split())


def evidence_in_body(evidence: str | None, bodies: Sequence[str]) -> bool | None:
    """证据原句是否逐字出现在正文里；没有正文时返回 None（无法判断）。"""
    wanted = collapse_spaces(evidence)
    if not bodies:
        return None
    return bool(wanted) and any(wanted in collapse_spaces(body) for body in bodies)


@dataclass
class Check:
    status: str
    reasons: list[str] = field(default_factory=list)

    def text(self) -> str:
        return self.status + (f"：{'；'.join(self.reasons)}" if self.reasons else "")


def _combine(checks: list[Check]) -> Check:
    failed = [r for c in checks if c.status == FAIL for r in c.reasons]
    if failed:
        return Check(FAIL, failed)
    unknown = [r for c in checks if c.status == UNKNOWN for r in c.reasons]
    if unknown:
        return Check(UNKNOWN, unknown)
    return Check(PASS)


def _overall(checks: list[Check]) -> str:
    """程序核对：通过 / 不通过（原因） / 无法判断（原因）；不适用的不影响结论。"""
    labelled = [
        Check(c.status, [f"{mark}{r}" for r in c.reasons])
        for mark, c in zip("①②③", checks, strict=True)
        if c.status != NOT_APPLICABLE
    ]
    result = _combine(labelled)
    text = result.status + (f"（{'；'.join(result.reasons)}）" if result.reasons else "")
    if result.status == PASS and all(c.status == NOT_APPLICABLE for c in checks[1:]):
        # 没有已核对的截止日期和字段时只核对了来源，结论要写明，免得被当成整条核对通过。
        return f"{PASS}（只核对了来源：没有已核对的截止日期和字段）"
    return text


def _traceable(url: str | None) -> Check:
    if url and re.match(r"https?://\S", url.strip(), re.IGNORECASE):
        return Check(PASS)
    return Check(FAIL, ["原文网址缺失或不是 http(s) 网址"])


def _evidence_check(items: list[tuple[str, str, date | None]], bodies: list[str]) -> Check:
    """items：(说明, 证据原句, 证据里应有的日期)；日期为 None 的只核对证据在正文里。"""
    checks: list[Check] = []
    for label, evidence, day in items:
        if not collapse_spaces(evidence):
            checks.append(Check(FAIL, [f"{label}：没有证据原句"]))
            continue
        found = evidence_in_body(evidence, bodies)
        if found is False:
            checks.append(Check(FAIL, [f"{label}：证据原句在正文里找不到"]))
        elif found is None:
            checks.append(Check(UNKNOWN, [f"{label}：没有正文，无法核对证据原句"]))
        if day is not None and not date_in_text(day, evidence):
            checks.append(Check(FAIL, [f"{label}：证据原句里找不到这个日期"]))
    return _combine(checks)


# ---------------------------------------------------------------- 抽样


def _round_robin(groups: dict[Hashable, list], n: int, rng: random.Random) -> list:
    """分层轮流抽：每组内先打乱，组的顺序也打乱，再每组轮流取一个，直到取够 n 个。
    调用方传入已排好序的组和成员，结果只取决于 seed。"""
    queues = []
    for key in sorted(groups):
        members = list(groups[key])
        rng.shuffle(members)
        queues.append(members)
    rng.shuffle(queues)
    picked: list = []
    depth = 0
    while len(picked) < n and any(depth < len(q) for q in queues):
        for queue in queues:
            if depth < len(queue) and len(picked) < n:
                picked.append(queue[depth])
        depth += 1
    return picked


def _safe_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def to_csv(headers: Sequence[str], rows: list[dict[str, object]]) -> bytes:
    """UTF-8 带 BOM 的 CSV（Excel 直接打开不乱码），第一行是中文表头。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_safe_cell(row.get(h, "")) for h in headers])
    return buffer.getvalue().encode("utf-8-sig")


def _deadline_text(d) -> str:
    when = d.approved_date.isoformat() + (
        f" {d.approved_time.strftime('%H:%M')}" if d.approved_time else ""
    )
    return f"{d.kind} {when}"


def _bodies(session: Session, members: dict[int, list[int]]) -> dict[int, list[str]]:
    """每份主文件（含同一通知的其他网址）抓到的正文原文。"""
    owner = {member: master for master, ids in members.items() for member in ids}
    rows = session.execute(
        select(DocumentDetail.document_id, DocumentDetail.body_text).where(
            DocumentDetail.document_id.in_(list(owner)),
            DocumentDetail.status == "ok",
        )
    ).all()
    result: dict[int, list[str]] = {master: [] for master in members}
    for document_id, body in sorted(rows):
        if body and body.strip():
            result[owner[document_id]].append(body)
    return result


def sample_notices(session: Session, n: int = 200, seed: int = 1) -> list[dict[str, object]]:
    """从小程序能看到的申报通知里按来源分层抽 n 份，列出对外展示的内容并做程序核对。

    范围与小程序一致：可以公开（未下架、一级来源或与一级来源归并）、同一通知只取主文件、
    类型为申报通知。截止日期只取人工核对通过的（与详情页同一函数），字段只取核对通过且
    正文没变过的。
    """
    rows = session.execute(_base().where(_kind_is(["申报通知"])).order_by(Document.id)).all()
    by_source: dict[int, list[int]] = {}
    sources: dict[int, Source] = {}
    docs: dict[int, Document] = {}
    for doc, src in rows:
        by_source.setdefault(src.id, []).append(doc.id)
        sources[doc.id], docs[doc.id] = src, doc
    picked = _round_robin(by_source, n, random.Random(seed))
    if not picked:
        return []
    details = _details(session, picked)
    approved = _approved(session, picked)
    fields = _approved_fields(session, picked)
    bodies = _bodies(session, groups_for(session, picked))

    out: list[dict[str, object]] = []
    for index, doc_id in enumerate(picked, 1):
        doc, src, detail = docs[doc_id], sources[doc_id], details.get(doc_id)
        deadlines = approved.get(doc_id, [])
        item = fields.get(doc_id, {})
        texts = bodies.get(doc_id, [])

        field_rows: list[tuple[str, str, str]] = [
            ("支持方式", value, item.get("support_evidence", {}).get(value, ""))
            for value in item.get("support", [])
        ]
        if "audience" in item:
            field_rows.append(("申报对象", item["audience"]["text"], item["audience"]["evidence"]))

        checks = [
            _traceable(doc.url),
            _evidence_check(
                [(_deadline_text(d), d.evidence, d.approved_date) for d in deadlines], texts
            )
            if deadlines
            else Check(NOT_APPLICABLE, ["没有已核对的截止日期"]),
            _evidence_check(
                [(f"{name} {value}", ev, None) for name, value, ev in field_rows], texts
            )
            if field_rows
            else Check(NOT_APPLICABLE, ["没有已核对的字段"]),
        ]
        out.append(
            {
                "序号": index,
                "文件编号": doc.id,
                "标题": doc.title,
                "来源名称": f"{src.dept_full_name} · {src.column_name}",
                "发文机关": detail.issuer if detail and detail.issuer else src.dept_full_name,
                "文号": detail.doc_number if detail and detail.doc_number else "",
                "发布日期": doc.publish_date.isoformat() if doc.publish_date else "",
                "原文网址": doc.url,
                "截止日期（已核对）": "\n".join(_deadline_text(d) for d in deadlines),
                "截止证据原句": "\n".join(f"{d.kind}：{d.evidence}" for d in deadlines),
                "已核对字段": "\n".join(f"{name}：{value}" for name, value, _ in field_rows),
                "字段证据原句": "\n".join(f"{value}：{ev}" for _, value, ev in field_rows),
                "正文": f"有（{max(len(t) for t in texts)} 字）" if texts else "无",
                "核对①来源可追溯": checks[0].text(),
                "核对②截止证据": checks[1].text(),
                "核对③字段证据": checks[2].text(),
                "程序核对": _overall(checks),
                "人工结论": "",
                "备注": "",
            }
        )
    return out


# ---------------------------------------------------------------- 名单比对

# 只改动名称主体，保留结尾的组织形式（股份有限公司、研究院等）
_ORG_ENDINGS = (
    "股份有限公司",
    "有限责任公司",
    "集团有限公司",
    "有限公司",
    "(有限合伙)",
    "(普通合伙)",
    "集团公司",
    "分公司",
    "公司",
    "研究院",
    "研究所",
    "研究中心",
    "中心",
    "合作社",
    "事务所",
    "大学",
    "学院",
    "医院",
    "实验室",
    "工作室",
    "厂",
)
_PLACES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
    "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "广西", "海南", "四川",
    "贵州", "云南", "陕西", "甘肃", "青海", "宁夏", "新疆", "内蒙古", "西藏", "广州", "深圳",
    "杭州", "南京", "苏州", "无锡", "宁波", "厦门", "青岛", "济南", "郑州", "武汉", "长沙",
    "成都", "西安", "合肥", "福州", "沈阳", "大连", "昆明", "南昌", "石家庄", "太原", "东莞",
    "佛山", "常州", "温州", "烟台", "珠海",
)  # fmt: skip
_WORDS = (
    "新材料", "新能源", "自动化", "科技", "技术", "信息", "电子", "智能", "网络", "软件", "生物",
    "医药", "材料", "环保", "能源", "机械", "设备", "装备", "制造", "工程", "实业", "发展",
    "控股", "集团", "股份", "数字", "数据", "光电", "精密", "化工", "汽车", "通信", "电气",
    "仪器", "科学", "创新", "工业", "贸易", "食品", "农业",
)  # fmt: skip
_SWAP_CHARS = "华恒泰盛达康瑞鑫源宏晟博锦卓信诚嘉"
_CJK = re.compile(r"[一-鿿]")


def _split_name(name: str) -> tuple[str, str]:
    for ending in _ORG_ENDINGS:
        if name.endswith(ending) and len(name) > len(ending):
            return name[: -len(ending)], ending
    return name, ""


def _place_prefix(core: str) -> str | None:
    return max((p for p in _PLACES if core.startswith(p)), key=len, default=None)


def _replace_char(name: str, rng: random.Random) -> str | None:
    core, ending = _split_name(name)
    positions = [m.start() for m in _CJK.finditer(core)]
    if not positions:
        return None
    pos = rng.choice(positions)
    choices = [c for c in _SWAP_CHARS if c != core[pos]]
    return core[:pos] + rng.choice(choices) + core[pos + 1 :] + ending


def _swap_place(name: str, rng: random.Random) -> str | None:
    core, ending = _split_name(name)
    prefix = _place_prefix(core)
    other = rng.choice([p for p in _PLACES if p != prefix])
    return (other + core[len(prefix) :] if prefix else other + core) + ending


def _drop_word(name: str, rng: random.Random) -> str | None:
    core, ending = _split_name(name)
    spots = sorted(
        {(m.start(), word) for word in _WORDS for m in re.finditer(re.escape(word), core)}
    )
    spots = [(i, w) for i, w in spots if len(core) - len(w) >= 2]
    if not spots:
        return None
    start, word = rng.choice(spots)
    return core[:start] + core[start + len(word) :] + ending


def _add_branch(name: str, rng: random.Random) -> str | None:
    if name.endswith("分公司"):
        return None
    prefix = _place_prefix(name)
    return name + rng.choice([p for p in _PLACES if p != prefix]) + "分公司"


NEAR_METHODS = (
    ("替换一个字", _replace_char),
    ("换城市前缀", _swap_place),
    ("去掉一个词", _drop_word),
    ("加分公司", _add_branch),
)


def near_variant(name: str, rng: random.Random, taken: set[str]) -> tuple[str, str] | None:
    """为真实名称生成一个“近似但不同”的名称，返回 (名称, 生成方式)。

    结果归一化后一定和原名不同，也不在 ``taken``（已有的真实名称和已生成的名称）里；
    像个人姓名的不要（名单比对不查）。四种方式从随机的一种开始依次尝试，都不行返回 None。
    """
    base = normalize_name(name)
    start = rng.randrange(len(NEAR_METHODS))
    for offset in range(len(NEAR_METHODS)):
        label, make = NEAR_METHODS[(start + offset) % len(NEAR_METHODS)]
        variant = make(base, rng)
        if variant is None:
            continue
        normalized = normalize_name(variant)
        if (
            normalized
            and normalized != base
            and normalized not in taken
            and not is_person_query(normalized)
            and normalize_credit_code(normalized) is None
        ):
            return variant, label
    return None


def _public_names(session: Session) -> dict[str, tuple[str, int]]:
    """名单比对能查到的全部名称：{归一化名称: (原文名称, 所在名单的主文件编号)}。"""
    rows = session.execute(
        select(ListEntry.normalized_name, func.min(ListEntry.name), func.min(ListEntry.document_id))
        .join(Document, Document.id == ListEntry.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            public_condition(),
            or_(Document.kind_override.is_(None), Document.kind_override == "名单"),
        )
        .group_by(ListEntry.normalized_name)
    ).all()
    masters = canonical_ids(session, sorted({doc_id for _, _, doc_id in rows}))
    return {
        normalized: (name, masters.get(doc_id, doc_id))
        for normalized, name, doc_id in rows
        if normalized
    }


def _lookup_row(session: Session, value: str) -> tuple[str, list[tuple[int, str]]]:
    """按名单比对（compare_lists）的口径查一个名称：结果和 [(名单主文件编号, 命中的名称)]。"""
    lookup = lookup_list_matches(session, value, scan_limit=None, result_limit=None)
    hits = [(m.master_id, m.name) for m in lookup.matches]
    status = "多个同名" if lookup.ambiguous else "命中" if hits else "未命中"
    return status, hits


def sample_compare(session: Session, n: int = 200, seed: int = 1) -> list[dict[str, object]]:
    """名单比对抽检：n/2 个真实企业名（按名单分层抽）、n/2 个近似但不同的名称，逐个比对。

    程序判断：真实名未命中 → 漏查待人工看；近似名命中且命中名称和输入不完全相同 →
    可能误匹配待人工看。名称不同但只差空白、全半角括号的（归一化后相同）算相同。
    """
    names = _public_names(session)
    by_list: dict[int, list[str]] = {}
    for normalized in sorted(names):
        if is_person_query(normalized):
            continue
        by_list.setdefault(names[normalized][1], []).append(normalized)
    rng = random.Random(seed)
    picked = _round_robin(by_list, n, rng)
    real_count, near_count = n - n // 2, n // 2
    real = picked[:real_count]
    bases = picked[real_count : real_count + near_count]
    if len(bases) < near_count:
        # 名称不够 n 个时，近似名也从真实名里取依据（生成的名称本身不会重复）
        bases += picked[: near_count - len(bases)]

    taken = set(names)
    inputs: list[tuple[str, bool, str, str]] = [(names[k][0], True, "", "") for k in real]
    for key in bases:
        made = near_variant(names[key][0], rng, taken)
        if made is not None:
            taken.add(normalize_name(made[0]))
            inputs.append((made[0], False, made[1], names[key][0]))

    results = [_lookup_row(session, value) for value, *_ in inputs]
    titles = dict(
        session.execute(
            select(Document.id, Document.title).where(
                Document.id.in_({doc_id for _, hits in results for doc_id, _ in hits})
            )
        ).all()
    )
    out: list[dict[str, object]] = []
    for index, ((value, is_real, method, base), (status, hits)) in enumerate(
        zip(inputs, results, strict=True), 1
    ):
        query = normalize_name(value)
        hit_names = list(dict.fromkeys(name for _, name in hits))
        if is_real:
            judgment = (
                "漏查待人工看"
                if not hits
                else "多个同名待人工看"
                if status == "多个同名"
                else "通过"
            )
        elif any(normalize_name(name) != query for name in hit_names):
            judgment = "可能误匹配待人工看"
        else:
            judgment = "命中同名（近似名恰是名单原名）" if hits else "通过"
        out.append(
            {
                "序号": index,
                "输入名称": value,
                "是否真实名": "是" if is_real else "否",
                "近似名生成方式": method,
                "近似名依据（真实名称）": base,
                "比对结果": status,
                "命中名单": "\n".join(
                    f"#{doc_id} {titles.get(doc_id, '')}"
                    for doc_id in dict.fromkeys(i for i, _ in hits)
                ),
                "命中的名称": "\n".join(hit_names),
                "程序判断": judgment,
                "人工结论": "",
                "备注": "",
            }
        )
    return out


KINDS = {"notices": (sample_notices, NOTICE_HEADERS), "compare": (sample_compare, COMPARE_HEADERS)}


def sample_csv(session: Session, kind: str, n: int = 200, seed: int = 1) -> tuple[bytes, int]:
    """生成抽检表 CSV，返回 (文件内容, 数据行数)。kind：notices 通知 / compare 名单比对。"""
    sampler, headers = KINDS[kind]
    rows = sampler(session, n=n, seed=seed)
    return to_csv(headers, rows), len(rows)
