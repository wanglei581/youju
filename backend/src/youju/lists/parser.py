"""从表格的企业名称列读取最少必要字段，丢弃所有其他列。"""

import io
import re
import zipfile
from collections.abc import Iterable
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


def _cell(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).replace("\xa0", " ").split()) if value is not None else ""


def _header_index(headers: list[str], terms: tuple[str, ...]) -> int | None:
    for i, header in enumerate(headers):
        if any(term in header for term in terms) and not any(
            term in header for term in _PERSON_HEADERS
        ):
            return i
    return None


def parse_table_rows(rows: Iterable[list[object] | tuple[object, ...]]) -> list[ParsedEntry]:
    """仅在明确的企业/机构名称表头之下抽取，避免把人员名单误收。"""
    output: list[ParsedEntry] = []
    headers: list[str] | None = None
    indices: tuple[int, int | None, int | None, int | None] | None = None
    for row in islice(rows, _MAX_ROWS):
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


def parse_html_tables(text: str) -> list[ParsedEntry]:
    root = html.fromstring(text)
    entries: list[ParsedEntry] = []
    for table in root.xpath("//table[not(ancestor::table)]"):
        rows = [
            [cell.text_content() for cell in tr.xpath("./th|./td")] for tr in table.xpath(".//tr")
        ]
        entries.extend(parse_table_rows(rows))
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
    extension = extension.lower().lstrip(".")
    entries: list[ParsedEntry] = []
    if extension == "pdf":
        if not data.startswith(b"%PDF-"):
            raise ValueError("不是 PDF 文件")
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            rows: list[list[object]] = []
            has_text = False
            for page in pdf.pages:
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
                raise ValueError("暂不支持扫描件")
            entries.extend(parse_table_rows(rows))
    elif extension == "xlsx":
        _check_zip(data)
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                entries.extend(parse_table_rows(sheet.iter_rows(values_only=True)))
        finally:
            workbook.close()
    elif extension == "xls":
        workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
        for sheet in workbook.sheets():
            entries.extend(
                parse_table_rows([sheet.row_values(i) for i in range(min(sheet.nrows, _MAX_ROWS))])
            )
        workbook.release_resources()
    elif extension == "docx":
        _check_zip(data)
        document = DocxDocument(io.BytesIO(data))
        for table in document.tables:
            entries.extend(
                parse_table_rows([[cell.text for cell in row.cells] for row in table.rows])
            )
    elif extension in {"doc", "wps", "et", "ofd"}:
        raise ValueError("暂不支持旧版或专有格式")
    else:
        raise ValueError("暂不支持此附件格式")
    return _unique(entries)
