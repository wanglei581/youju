"""从表格的企业名称列读取最少必要字段，丢弃所有其他列。"""

import io
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import islice

import pdfplumber
import xlrd
from docx import Document as DocxDocument
from lxml import html
from openpyxl import load_workbook

from youju.lists import doc97
from youju.lists.rules import normalize_credit_code, normalize_name, valid_org_name

_NAME_HEADERS = (
    "企业名称",
    "单位名称",
    "机构名称",
    "主体名称",
    "公司名称",
    "申报单位",
    "企业全称",
    "工厂名称",
)
# 整格就写这几个字的也是名称列（10/16：福建水效领跑者名单表头是"企业"）。只认整格相等：
# "企业类型""企业规模"之类不算；不收单写"单位"的：获奖名单里常是获奖人所在单位，不是上榜的机构
_NAME_HEADERS_EXACT = (
    "企业",
    "企业(单位)",
    "单位(企业)",
    "申报企业",
    "推荐企业",
    "获奖企业",
    "入选企业",
)
_CODE_HEADERS = ("统一社会信用代码", "统一信用代码", "社会信用代码")
_REGION_HEADERS = ("所在地", "注册地", "所属地区", "所属区市", "地区", "地市", "区县", "所属区县")
_PERSON_HEADERS = ("联系人", "法定代表人", "负责人", "姓名", "电话", "身份证", "手机号", "银行账号")
_MAX_ROWS = 100_000
_MAX_COLUMNS = 256
_MAX_CELLS = 1_000_000
_MAX_TEXT_CHARS = 8 * 1024 * 1024


@dataclass
class _TableBudget:
    # 整份文件共用预算；稀疏 Excel 的声明范围也可能展开成巨大的空矩阵。
    rows: int = 0
    cells: int = 0
    text_chars: int = 0

    def read(self, rows: Iterable[list[object] | tuple[object, ...]]) -> list[list[object]]:
        result = []
        for row in rows:
            self.rows += 1
            self.cells += len(row)
            if len(row) > _MAX_COLUMNS or self.rows > _MAX_ROWS or self.cells > _MAX_CELLS:
                raise ValueError("暂不支持超过安全解析预算的表格（行列或单元格过多）")
            self.text_chars += sum(len(str(value)) for value in row if value is not None)
            if self.text_chars > _MAX_TEXT_CHARS:
                raise ValueError("暂不支持超过安全解析预算的表格（文字过多）")
            result.append(list(row))
        return result


# 单个超长 PDF 的逐页表格识别很耗时；最多处理前 300 页，避免阻塞整批回填。
# PDF 最多读几页（10/17 创始人批准由 300 调到 1000）。300 是 9/28 定的，
# 为的是不让一份超长附件拖慢整批解析；逐页释放缓存后内存不随页数涨。
# 河南 567 页的入库名单本机实测：300 页读出 6298 家，全读 11903 家、13 秒、83 MB。
# 以前被截断的，service._should_attempt 按原因里的页数重读一次
MAX_PDF_PAGES = 1000
_MAX_ZIP_UNCOMPRESSED = 100 * 1024 * 1024
_REGION = re.compile(r"[\u4e00-\u9fffA-Za-z0-9（）()·\-/ ]{2,64}")
_REGION_SUFFIX = ("省", "市", "区", "县", "州", "盟", "旗", "地区", "新区", "开发区", "园区")
_REGION_EXACT = {"北京", "上海", "天津", "重庆", "新疆", "西藏", "宁夏", "内蒙古", "香港", "澳门"}


@dataclass(frozen=True)
class ParsedEntry:
    name: str
    normalized_name: str
    credit_code: str | None
    sequence: str | None
    region: str | None


# 规则按表头找不到名称列时，请 AI 认列：传入（表头, 数据行），返回名称列的列号或 None
NameColumnPicker = Callable[[list[str], list[list[str]]], int | None]


@dataclass(frozen=True)
class AttachmentParse:
    entries: list[ParsedEntry]
    failure_reason: str | None = None


# 两个汉字之间的空白：表格格子里名称折行（"……有限\n公司"）读出来的，中文名称里本来没有
_HAN_GAP = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def _cell(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if value is None:
        return ""
    return _HAN_GAP.sub("", " ".join(str(value).replace("\xa0", " ").split()))


# 表头里括号中的补充说明（"企业（团队）名称""单位名称（盖章）"）比对名称列时去掉；
# 表头已经过 normalize_name，全角括号换成了半角
_HEADER_NOTE = re.compile(r"\([^()]*\)")


def _header_index(
    headers: list[str], terms: tuple[str, ...], exact: tuple[str, ...] = ()
) -> int | None:
    for i, header in enumerate(headers):
        bare = _HEADER_NOTE.sub("", header)
        named = any(term in header or term in bare for term in terms) or header in exact
        if named and not any(term in header for term in _PERSON_HEADERS):
            return i
    return None


def parse_table_rows(
    rows: Iterable[list[object] | tuple[object, ...]],
    pick_name_column: NameColumnPicker | None = None,
    *,
    _budget: _TableBudget | None = None,
) -> list[ParsedEntry]:
    """仅在明确的企业/机构名称表头之下抽取，避免把人员名单误收。

    规则一条也没抽到、又传了 pick_name_column 时，请 AI 认出名称列再抽（见 youju.ai.tables）；
    抽出的名称同样要通过机构名称规则。
    """
    materialized = (_budget or _TableBudget()).read(rows)
    return _parse_bounded_rows(materialized, pick_name_column)


def _parse_bounded_rows(
    materialized: list[list[object]], pick_name_column: NameColumnPicker | None
) -> list[ParsedEntry]:
    output = _rule_entries(materialized)
    if output or pick_name_column is None:
        return output
    return _picked_entries(materialized, pick_name_column)


def _picked_entries(rows: list[list[object]], pick: NameColumnPicker) -> list[ParsedEntry]:
    cells = [[_cell(value) for value in row] for row in rows]
    non_empty = [row for row in cells if any(row)]
    if len(non_empty) < 3:  # 至少一行表头（可能缺）和两行数据
        return []
    headers = non_empty[0]
    index = pick(headers, non_empty[1:51])
    if index is None:
        return []
    names = [normalize_name(value) for value in headers]
    code_i = _header_index(names, _CODE_HEADERS)
    output: list[ParsedEntry] = []
    # 第一行可能不是表头而是数据（表头缺失时），照样检查；真表头过不了机构名称规则
    for row in non_empty:
        if index >= len(row):
            continue
        raw_name = row[index].strip()
        code = (
            normalize_credit_code(row[code_i]) if code_i is not None and code_i < len(row) else None
        )
        if valid_org_name(raw_name, code):
            output.append(
                ParsedEntry(
                    name=raw_name,
                    normalized_name=normalize_name(raw_name),
                    credit_code=code,
                    sequence=None,
                    region=None,
                )
            )
    return output


def _rule_entries(rows: list[list[object]]) -> list[ParsedEntry]:
    output: list[ParsedEntry] = []
    headers: list[str] | None = None
    indices: tuple[int, int | None, int | None, int | None] | None = None
    for row in rows:
        cells = [_cell(value) for value in row]
        current = [normalize_name(value) for value in cells]
        name_index = _header_index(current, _NAME_HEADERS, _NAME_HEADERS_EXACT)
        if name_index is not None:
            headers = current
            indices = (
                name_index,
                _header_index(headers, _CODE_HEADERS),
                _header_index(headers, _REGION_HEADERS),
                _header_index(headers, ("序号",)),
            )
            continue
        if indices is None or headers is None:
            continue
        name_i, code_i, region_i, sequence_i = indices
        if name_i >= len(cells):
            continue
        raw_name = cells[name_i].strip()
        code = (
            normalize_credit_code(cells[code_i])
            if code_i is not None and code_i < len(cells)
            else None
        )
        if not valid_org_name(raw_name, code):
            continue
        sequence = cells[sequence_i] if sequence_i is not None and sequence_i < len(cells) else ""
        sequence = sequence if re.fullmatch(r"\d{1,8}", sequence) else None
        region = cells[region_i].strip() if region_i is not None and region_i < len(cells) else ""
        region = (
            region
            if _REGION.fullmatch(region)
            and all(
                part.endswith(_REGION_SUFFIX) or part in _REGION_EXACT for part in region.split("/")
            )
            else None
        )
        output.append(
            ParsedEntry(
                name=raw_name,
                normalized_name=normalize_name(raw_name),
                credit_code=code,
                sequence=sequence,
                region=region,
            )
        )
    return output


def _unique(entries: list[ParsedEntry]) -> list[ParsedEntry]:
    seen: set[tuple[str, str | None]] = set()
    result: list[ParsedEntry] = []
    for entry in entries:
        key = (entry.normalized_name, entry.sequence)
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


def parse_html_tables(
    text: str, pick_name_column: NameColumnPicker | None = None
) -> list[ParsedEntry]:
    root = html.fromstring(text)
    entries: list[ParsedEntry] = []
    budget = _TableBudget()
    for table in root.xpath("//table[not(ancestor::table)]"):
        rows = (
            [cell.text_content() for cell in tr.xpath("./th|./td")] for tr in table.xpath(".//tr")
        )
        entries.extend(parse_table_rows(rows, pick_name_column, _budget=budget))
    return _unique(entries)


def _check_zip(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if (
            len(members) > 1_000
            or sum(member.file_size for member in members) > _MAX_ZIP_UNCOMPRESSED
        ):
            raise ValueError("压缩文件展开后过大")


def _docx_entries(
    data: bytes, pick_name_column: NameColumnPicker | None, budget: _TableBudget
) -> list[ParsedEntry]:
    _check_zip(data)
    document = DocxDocument(io.BytesIO(data))
    entries: list[ParsedEntry] = []
    for table in document.tables:
        # python-docx 的 row.cells 会按 gridSpan 展开引用；先检查原始 XML，
        # 避免一格声明超大横向合并就在预算生效前制造巨型 tuple。
        for row in table.rows:
            spans = [cell.grid_span for cell in row._tr.tc_lst]
            if any(span < 1 for span in spans) or sum(spans) > _MAX_COLUMNS:
                raise ValueError("暂不支持超过安全解析预算的表格（合并列过多）")
        entries.extend(
            parse_table_rows(
                ([cell.text for cell in row.cells] for row in table.rows),
                pick_name_column,
                _budget=budget,
            )
        )
    return entries


_META_CHARSET = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)", re.I)


def _decode_html(data: bytes) -> str:
    """存成 .doc 的网页：按 meta 声明的编码解码（政府网站常见 GBK/GB2312）；
    没声明就先试 UTF-8，再按 GB18030。"""
    match = _META_CHARSET.search(data[:4096])
    if match:
        name = match.group(1).decode("ascii").lower()
        if name in {"gbk", "gb2312", "gb18030", "x-gbk"}:
            return data.decode("gb18030", "replace")
        try:
            return data.decode(name, "replace")
        except LookupError:
            pass
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", "replace")


def _word97_entries(
    data: bytes, pick_name_column: NameColumnPicker | None, budget: _TableBudget
) -> list[ParsedEntry]:
    """.doc / .wps：按文件内容判断。实际是 DOCX（改了扩展名）就按 DOCX 读，是网页就读网页表格，
    RTF 不支持；Word 97 二进制按 doc97 还原表格（10/16 起）。"""
    head = data[:512].lstrip().lower()
    if data.startswith(b"PK"):
        return _docx_entries(data, pick_name_column, budget)
    if head.startswith(b"{\\rtf"):
        raise ValueError("暂不支持 RTF 格式")
    if head.startswith((b"<!doctype html", b"<html")):
        return parse_html_tables(_decode_html(data), pick_name_column)
    if not doc97.is_ole(data):
        # 带括号说明：和以前代码记的原话区分开，补解析一次以后不再重试（service.LEGACY_UNSUPPORTED）
        raise ValueError("暂不支持旧版或专有格式（不是 Word 97 文档）")
    entries: list[ParsedEntry] = []
    for rows in doc97.doc_tables(doc97.doc_text(data)):
        entries.extend(parse_table_rows(rows, pick_name_column, _budget=budget))
    return entries


def parse_attachment(data: bytes, extension: str) -> list[ParsedEntry]:
    """支持可读取表格的 PDF/XLSX/XLS/DOCX/DOC/WPS（Word 97 二进制）；ET、OFD 和扫描件留失败原因。"""
    return parse_attachment_result(data, extension).entries


def parse_attachment_result(
    data: bytes, extension: str, pick_name_column: NameColumnPicker | None = None
) -> AttachmentParse:
    """返回条目及 PDF 截断原因，供每份附件的解析记录保存。"""
    extension = extension.lower().lstrip(".")
    entries: list[ParsedEntry] = []
    failure_reason = None
    budget = _TableBudget()
    if extension == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("不是 PDF 文件")
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            rows: list[list[object]] = []
            has_text = False
            page_count = len(pdf.pages)
            if page_count > MAX_PDF_PAGES:
                failure_reason = f"PDF 共{page_count}页，仅解析前{MAX_PDF_PAGES}页"
            for page in islice(pdf.pages, MAX_PDF_PAGES):
                try:
                    has_text = has_text or bool((page.extract_text() or "").strip())
                    tables = page.extract_tables()
                    if not tables:
                        tables = page.extract_tables(
                            table_settings={
                                "vertical_strategy": "text",
                                "horizontal_strategy": "text",
                            }
                        )
                    for table in tables:
                        rows.extend(budget.read(table))
                finally:
                    # pdfplumber 缓存布局和文本对象；异常或超预算也必须释放当前页。
                    page.close()
            if not has_text:
                reason = f"；{failure_reason}" if failure_reason else ""
                raise ValueError(f"暂不支持扫描件{reason}")
            entries.extend(_parse_bounded_rows(rows, pick_name_column))
    elif extension == "xlsx":
        _check_zip(data)
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                # dimension 可缺失或不准确。重置后缺失行为空，不按声明宽度补齐矩阵，
                # 也不因过小声明漏掉真实数据；实际行、列仍由共享预算拒绝。
                sheet.reset_dimensions()
                entries.extend(
                    parse_table_rows(
                        sheet.iter_rows(values_only=True), pick_name_column, _budget=budget
                    )
                )
        finally:
            workbook.close()
    elif extension == "xls":
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True, ragged_rows=True)
        try:
            # sheets() 会同时加载全部工作表；ragged_rows 防止稀疏短行补齐到最宽列。
            for index in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(index)
                try:
                    if sheet.ncols > _MAX_COLUMNS or sheet.nrows > _MAX_ROWS:
                        raise ValueError("暂不支持超过安全解析预算的表格（声明行列过多）")
                    entries.extend(
                        parse_table_rows(
                            (sheet.row_values(i) for i in range(sheet.nrows)),
                            pick_name_column,
                            _budget=budget,
                        )
                    )
                finally:
                    workbook.unload_sheet(index)
        finally:
            workbook.release_resources()
    elif extension == "docx":
        entries.extend(_docx_entries(data, pick_name_column, budget))
    elif extension in {"doc", "wps"}:
        entries.extend(_word97_entries(data, pick_name_column, budget))
    elif extension in {"et", "ofd"}:
        raise ValueError("暂不支持旧版或专有格式")
    else:
        raise ValueError("暂不支持此附件格式")
    return AttachmentParse(_unique(entries), failure_reason)
