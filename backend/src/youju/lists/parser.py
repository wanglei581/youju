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

from youju.lists.rules import normalize_credit_code, normalize_name, valid_org_name

_NAME_HEADERS = ("企业名称", "单位名称", "机构名称", "主体名称", "公司名称", "申报单位", "企业全称")
_CODE_HEADERS = ("统一社会信用代码", "统一信用代码", "社会信用代码")
_REGION_HEADERS = ("所在地", "注册地", "所属地区", "所属区市", "地区", "地市", "区县", "所属区县")
_PERSON_HEADERS = ("联系人", "法定代表人", "负责人", "姓名", "电话", "身份证", "手机号", "银行账号")
_MAX_ROWS = 100_000
# 单个超长 PDF 的逐页表格识别很耗时；最多处理前 300 页，避免阻塞整批回填。
MAX_PDF_PAGES = 300
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


def _header_index(headers: list[str], terms: tuple[str, ...]) -> int | None:
    for i, header in enumerate(headers):
        if any(term in header for term in terms) and not any(
            term in header for term in _PERSON_HEADERS
        ):
            return i
    return None


def parse_table_rows(
    rows: Iterable[list[object] | tuple[object, ...]],
    pick_name_column: NameColumnPicker | None = None,
) -> list[ParsedEntry]:
    """仅在明确的企业/机构名称表头之下抽取，避免把人员名单误收。

    规则一条也没抽到、又传了 pick_name_column 时，请 AI 认出名称列再抽（见 youju.ai.tables）；
    抽出的名称同样要通过机构名称规则。
    """
    materialized = [list(row) for row in islice(rows, _MAX_ROWS)]
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
        name_index = _header_index(current, _NAME_HEADERS)
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
    for table in root.xpath("//table[not(ancestor::table)]"):
        rows = [
            [cell.text_content() for cell in tr.xpath("./th|./td")] for tr in table.xpath(".//tr")
        ]
        entries.extend(parse_table_rows(rows, pick_name_column))
    return _unique(entries)


def _check_zip(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if (
            len(members) > 1_000
            or sum(member.file_size for member in members) > _MAX_ZIP_UNCOMPRESSED
        ):
            raise ValueError("压缩文件展开后过大")


def parse_attachment(data: bytes, extension: str) -> list[ParsedEntry]:
    """支持可读取表格的 PDF/XLSX/XLS/DOCX；旧 DOC 和扫描件留失败原因。"""
    return parse_attachment_result(data, extension).entries


def parse_attachment_result(
    data: bytes, extension: str, pick_name_column: NameColumnPicker | None = None
) -> AttachmentParse:
    """返回条目及 PDF 截断原因，供每份附件的解析记录保存。"""
    extension = extension.lower().lstrip(".")
    entries: list[ParsedEntry] = []
    failure_reason = None
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
                has_text = has_text or bool((page.extract_text() or "").strip())
                tables = page.extract_tables()
                for table in tables:
                    rows.extend(table)
                if not tables:
                    for table in page.extract_tables(
                        table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
                    ):
                        rows.extend(table)
            if not has_text:
                reason = f"；{failure_reason}" if failure_reason else ""
                raise ValueError(f"暂不支持扫描件{reason}")
            entries.extend(parse_table_rows(rows, pick_name_column))
    elif extension == "xlsx":
        _check_zip(data)
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                entries.extend(
                    parse_table_rows(sheet.iter_rows(values_only=True), pick_name_column)
                )
        finally:
            workbook.close()
    elif extension == "xls":
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        for sheet in workbook.sheets():
            entries.extend(
                parse_table_rows(
                    [sheet.row_values(i) for i in range(min(sheet.nrows, _MAX_ROWS))],
                    pick_name_column,
                )
            )
        workbook.release_resources()
    elif extension == "docx":
        _check_zip(data)
        document = DocxDocument(io.BytesIO(data))
        for table in document.tables:
            entries.extend(
                parse_table_rows(
                    [[cell.text for cell in row.cells] for row in table.rows], pick_name_column
                )
            )
    elif extension in {"doc", "wps", "et", "ofd"}:
        raise ValueError("暂不支持旧版或专有格式")
    else:
        raise ValueError("暂不支持此附件格式")
    return AttachmentParse(_unique(entries), failure_reason)
