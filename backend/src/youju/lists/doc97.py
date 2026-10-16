"""读 Word 97–2003 二进制文档（.doc；金山 WPS 文字存的 .wps 也是这种格式）里的表格。

10/16 查"查名单"读不出企业名的原因时发现：河北"纳入科技型中小企业信息库"的两批名单
（8722 家、6124 家）、福建省级绿色制造名单等都是这种附件，以前一律记作"暂不支持旧版或专有格式"。
这里不调用 Office 或 LibreOffice（镜像要大几百 MB），只按格式规范（[MS-DOC]）读正文：

- 文档是 OLE 复合文件；WordDocument 流开头的 FIB 指出文字片段表（CLX）在 0Table 或 1Table
  流里的位置，片段表给出正文每一段文字在 WordDocument 流里的位置和编码
  （压缩的是单字节，否则是 UTF-16）；
- 只读正文（ccpText 之内），不读脚注、页眉；域代码（\\x13 指令 \\x14 结果 \\x15）
  只留显示出来的结果；
- 表格里每一格以 \\x07 结束，每行最后一格之后还有一个"行尾段落"（也是 \\x07）。哪个 \\x07 是行尾，
  看段落属性：行尾段落带 sprmPFTtp（表格终止段落）。不按格数猜（Codex 审查：一行多出空格子时
  按格数猜会错位，把推荐单位当成企业名称）；属性读不出来就不认表格。

读出的行交给 parser.parse_table_rows，和别的格式一样只在明确的企业/机构名称表头下取名称。
防恶意文件：正文最多 MAX_CHARS 字、片段最多 MAX_PIECES 段、域最多嵌套 MAX_FIELD_DEPTH 层，
位置越界、结构损坏一律报"暂不支持……"。
"""

from __future__ import annotations

import bisect
import io
import struct
from array import array

import olefile

CELL_MARK = "\x07"
# 行尾段落的 \x07 在读出的正文里换成这个字符（正文里本来不会有），表格按它断行
ROW_END = "\x1d"
MAX_CHARS = 5_000_000
MAX_PIECES = 100_000
MAX_FIELD_DEPTH = 64
MAX_FKP_PAGES = (
    10_000  # 段落属性页数：8722 行的名单约 1200 页；每页最多 29 段，上限时区间约 29 万个
)
_WORD97_MIN_NFIB = 0x00C1
# FibRgFcLcb97 里第 13 对是 PlcBtePapx（段落属性页），第 33 对是 Clx（文字片段表）；
# FibRgLw97 里第 3 个是 ccpText（正文字数）
_PAPX_PAIR = 13
_CLX_PAIR = 33
_CCP_TEXT = 3
_SPRM_PF_TTP = 0x2417  # 表格终止段落（行尾）
_SPRM_T_DEF_TABLE = 0xD608
_SPRM_P_CHG_TABS = 0xC615
_SPRM_P_HUGE_PAPX = 0x6646  # 段落属性太长时放在 Data 流里，这里只留指针
_FKP_SIZE = 512
# 格子里的换段、换行、分页、图片和隐藏连字符都当空格
_CELL_BLANKS = str.maketrans({ch: " " for ch in "\r\x0b\x0c\x01\x08\x1e\x1f\xa0"})


class Doc97Error(ValueError):
    """读不了的文档；消息以"暂不支持"开头，名单解析记录据此保存失败原因。"""


def is_ole(data: bytes) -> bool:
    return data[:8] == olefile.MAGIC


def doc_text(data: bytes) -> str:
    """正文文字：\\r 段落标记、\\x07 单元格结束、ROW_END 行尾；域代码只留结果。"""
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError as exc:
        raise Doc97Error("暂不支持旧版或专有格式（不是 Word 97 文档）") from exc
    try:
        if not ole.exists("WordDocument"):
            raise Doc97Error("暂不支持旧版或专有格式（不是 Word 97 文档）")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 34 or struct.unpack_from("<H", wd, 0)[0] != 0xA5EC:
            raise Doc97Error("暂不支持旧版或专有格式（不是 Word 97 文档）")
        if struct.unpack_from("<H", wd, 2)[0] < _WORD97_MIN_NFIB:
            raise Doc97Error("暂不支持旧版或专有格式（Word 95 及更早）")
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        if flags & 0x0100:
            raise Doc97Error("暂不支持加密文档")
        table_stream = "1Table" if flags & 0x0200 else "0Table"
        if not ole.exists(table_stream):
            raise Doc97Error("暂不支持旧版或专有格式（缺少表格流）")
        table = ole.openstream(table_stream).read()
        data_stream = ole.openstream("Data").read() if ole.exists("Data") else b""
        return _drop_field_codes(_main_text(wd, table, data_stream))
    except Doc97Error:
        raise
    except (struct.error, IndexError, OSError, ValueError) as exc:
        # 复合文件目录或扇区链损坏（olefile 读流时报 OSError）、结构字段越界
        raise Doc97Error("暂不支持旧版或专有格式（文档结构无法识别）") from exc
    finally:
        ole.close()


def _fib_pair(wd: bytes, index: int) -> tuple[int, int]:
    csw = struct.unpack_from("<H", wd, 32)[0]
    cslw = struct.unpack_from("<H", wd, 34 + csw * 2)[0]
    rgfclcb = 34 + csw * 2 + 2 + cslw * 4 + 2
    return struct.unpack_from("<II", wd, rgfclcb + index * 8)


def _ccp_text(wd: bytes) -> int:
    csw = struct.unpack_from("<H", wd, 32)[0]
    return struct.unpack_from("<i", wd, 34 + csw * 2 + 2 + _CCP_TEXT * 4)[0]


def _pieces(wd: bytes, table: bytes) -> list[tuple[int, int, int, bool]]:
    """正文的文字片段：(起始字符位置, 字数, 在 WordDocument 流里的字节位置, 是否单字节)。"""
    ccp_text = _ccp_text(wd)
    if not 0 <= ccp_text <= MAX_CHARS:
        raise Doc97Error("暂不支持超过安全解析预算的文档（正文过长）")
    fc_clx, lcb_clx = _fib_pair(wd, _CLX_PAIR)
    if fc_clx + lcb_clx > len(table):
        raise Doc97Error("暂不支持旧版或专有格式（文字片段表越界）")
    clx = table[fc_clx : fc_clx + lcb_clx]
    i = 0
    while i < len(clx) and clx[i] == 0x01:  # Prc：格式信息，跳过
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if i >= len(clx) or clx[i] != 0x02:
        raise Doc97Error("暂不支持旧版或专有格式（找不到文字片段表）")
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5 : i + 5 + lcb]
    count = (lcb - 4) // 12  # (n+1) 个字符位置各 4 字节 + n 个片段描述各 8 字节
    if not 1 <= count <= MAX_PIECES or len(plc) < (count + 1) * 4 + count * 8:
        raise Doc97Error("暂不支持旧版或专有格式（文字片段表不完整）")
    cps = struct.unpack_from(f"<{count + 1}I", plc, 0)
    if cps[0] != 0:
        raise Doc97Error("暂不支持旧版或专有格式（文字片段不从正文开头开始）")
    pieces: list[tuple[int, int, int, bool]] = []
    covered = 0
    for k in range(count):
        if cps[k + 1] <= cps[k]:
            raise Doc97Error("暂不支持旧版或专有格式（文字片段顺序错乱）")
        if cps[k] >= ccp_text:
            break
        fc = struct.unpack_from("<I", plc, (count + 1) * 4 + k * 8 + 2)[0]
        length = min(cps[k + 1], ccp_text) - cps[k]
        compressed = bool(fc & 0x40000000)
        offset = (fc & 0x3FFFFFFF) // 2 if compressed else fc
        if offset + length * (1 if compressed else 2) > len(wd):
            raise Doc97Error("暂不支持旧版或专有格式（文字片段越界）")
        pieces.append((cps[k], length, offset, compressed))
        covered += length
    # 片段要连起来盖满正文，不然缺的那段会被当成正常结果
    if covered < ccp_text:
        raise Doc97Error("暂不支持旧版或专有格式（文字片段不完整）")
    return pieces


def _main_text(wd: bytes, table: bytes, data_stream: bytes = b"") -> str:
    pieces = _pieces(wd, table)
    row_ends = _ttp_runs(wd, table, data_stream)
    parts: list[str] = []
    total = 0
    for _cp, length, offset, compressed in pieces:
        total += length
        if total > MAX_CHARS:
            raise Doc97Error("暂不支持超过安全解析预算的文档（正文过长）")
        width = 1 if compressed else 2
        raw = bytearray(wd[offset : offset + length * width])
        # 每个 \x07 在流里的字节位置落在哪个段落属性区间：带行尾标记的换成 ROW_END。
        # 在原始字节里找（UTF-16 只认偶数位置）：生僻字一个占 4 字节，按解码后的字符位置算会偏
        mark = b"\x07" if compressed else b"\x07\x00"
        pos = raw.find(mark)
        while pos != -1:
            if (compressed or pos % 2 == 0) and _is_row_end(row_ends, offset + pos):
                raw[pos] = ord(ROW_END)
            pos = raw.find(mark, pos + 1)
        parts.append(bytes(raw).decode("cp1252" if compressed else "utf-16-le", "replace"))
    return "".join(parts)


def _ttp_runs(wd: bytes, table: bytes, data_stream: bytes = b"") -> tuple[array, array, bytearray]:
    """段落属性页（PapxFkp）里的区间：(起点, 终点, 是否行尾段落)，按起点排好，供二分查找。"""
    fc_bte, lcb_bte = _fib_pair(wd, _PAPX_PAIR)
    if lcb_bte < 8 or fc_bte + lcb_bte > len(table):
        raise Doc97Error("暂不支持旧版或专有格式（段落属性表越界）")
    count = (lcb_bte - 4) // 8  # (n+1) 个位置各 4 字节 + n 个页号各 4 字节
    if count > MAX_FKP_PAGES:
        raise Doc97Error("暂不支持超过安全解析预算的文档（段落属性页过多）")
    # 同一页只读一次（正常文档不会重复，恶意文件可以反复指同一页）
    pages = dict.fromkeys(
        pn & 0x3FFFFF for pn in struct.unpack_from(f"<{count}I", table, fc_bte + (count + 1) * 4)
    )
    # 紧凑存放（#272 审阅：上万页时元组列表要上百 MB）
    starts, ends, ttp = array("I"), array("I"), bytearray()
    for page_number in pages:
        base = page_number * _FKP_SIZE
        if base + _FKP_SIZE > len(wd):
            raise Doc97Error("暂不支持旧版或专有格式（段落属性页越界）")
        page = wd[base : base + _FKP_SIZE]
        crun = page[_FKP_SIZE - 1]
        if not 1 <= crun <= 0x1D:  # 规范：段落属性页最多 29 段
            raise Doc97Error("暂不支持旧版或专有格式（段落属性页损坏）")
        fcs = struct.unpack_from(f"<{crun + 1}I", page, 0)
        for r in range(crun):
            bx = (crun + 1) * 4 + r * 13
            starts.append(fcs[r])
            ends.append(fcs[r + 1])
            ttp.append(_papx_is_ttp(page, page[bx] * 2, data_stream))
    # 规范要求按位置排好；乱序的（损坏或特制文件）按起点重排
    if any(starts[k] > starts[k + 1] for k in range(len(starts) - 1)):
        order = sorted(range(len(starts)), key=starts.__getitem__)
        starts = array("I", (starts[k] for k in order))
        ends = array("I", (ends[k] for k in order))
        ttp = bytearray(ttp[k] for k in order)
    return starts, ends, ttp


def _is_row_end(runs: tuple[array, array, bytearray], fc: int) -> bool:
    starts, ends, ttp = runs
    i = bisect.bisect_right(starts, fc) - 1
    return i >= 0 and fc < ends[i] and ttp[i]


def _papx_is_ttp(page: bytes, offset: int, data_stream: bytes = b"") -> bool:
    """PapxInFkp：段落属性里有没有"表格终止段落"（sprmPFTtp 值为 1）。"""
    if offset == 0:  # 没有段落属性
        return False
    cb = page[offset]
    if cb:
        start, size = offset + 1, 2 * cb - 1
    else:
        start, size = offset + 2, 2 * page[offset + 1]
    return _grpprl_has_ttp(page[start + 2 : start + size], data_stream)  # 前 2 字节是样式编号


def _grpprl_has_ttp(grpprl: bytes, data_stream: bytes, nested: bool = False) -> bool:
    i = 0
    while i + 2 <= len(grpprl):
        sprm = struct.unpack_from("<H", grpprl, i)[0]
        i += 2
        if sprm == _SPRM_PF_TTP:
            return i < len(grpprl) and grpprl[i] == 1
        if sprm == _SPRM_P_HUGE_PAPX:
            # 操作数是 Data 流里的位置：PrcData = 2 字节长度 + 段落属性（Codex 审查）
            if nested or i + 4 > len(grpprl):
                return False
            at = struct.unpack_from("<I", grpprl, i)[0]
            if at + 2 > len(data_stream):
                raise Doc97Error("暂不支持旧版或专有格式（段落属性指针越界）")
            size = struct.unpack_from("<h", data_stream, at)[0]
            if not 0 <= size <= 0x3FA2 or at + 2 + size > len(data_stream):
                raise Doc97Error("暂不支持旧版或专有格式（段落属性指针越界）")
            if _grpprl_has_ttp(data_stream[at + 2 : at + 2 + size], data_stream, nested=True):
                return True
            i += 4
            continue
        spra = sprm >> 13
        if spra in (0, 1):
            i += 1
        elif spra in (2, 4, 5):
            i += 2
        elif spra == 3:
            i += 4
        elif spra == 7:
            i += 3
        elif sprm == _SPRM_T_DEF_TABLE:  # 长度是后面的 2 字节值减 1，再加这 2 字节
            if i + 2 > len(grpprl):
                return False
            i += 1 + struct.unpack_from("<H", grpprl, i)[0]
        elif sprm == _SPRM_P_CHG_TABS and i < len(grpprl) and grpprl[i] == 255:
            # 特殊写法：删除的制表位 cTabs 个（各 4 字节），再是增加的 cTabs 个（各 3 字节）
            j = i + 1
            if j >= len(grpprl):
                return False
            j += 1 + 4 * grpprl[j]
            if j >= len(grpprl):
                return False
            i = j + 1 + 3 * grpprl[j]
        else:  # 长度在第一个字节
            if i >= len(grpprl):
                return False
            i += 1 + grpprl[i]
    return False


def _drop_field_codes(text: str) -> str:
    out: list[str] = []
    stack: list[bool] = []  # 每层域：False 在指令里（跳过），True 在结果里（保留）
    hidden = 0  # 有几层还在指令里：为 0 时这个字显示出来
    for ch in text:
        if ch == "\x13":
            if len(stack) >= MAX_FIELD_DEPTH:
                raise Doc97Error("暂不支持超过安全解析预算的文档（域嵌套过深）")
            stack.append(False)
            hidden += 1
        elif ch == "\x14" and stack:
            if not stack[-1]:
                stack[-1] = True
                hidden -= 1
        elif ch == "\x15" and stack:
            if not stack.pop():
                hidden -= 1
        elif hidden == 0:
            out.append(ch)
    return "".join(out)


def _clean(cell: str) -> str:
    return " ".join(cell.translate(_CELL_BLANKS).split())


def doc_tables(text: str) -> list[list[list[str]]]:
    """按行尾标记（ROW_END）把正文里的表格还原成行；一行里的格按 \\x07 分。只有一行的表不要。

    一行的第一格前面如果带着正文段落（以 \\r 结束），说明前一张表到此为止、这是新表的第一行，
    这一格只取最后一段。整行都空的不要。"""
    tables: list[list[list[str]]] = []
    rows: list[list[str]] = []
    # 最后一段在最后一个行尾标记之后，是表格后面的正文
    for chunk in text.split(ROW_END)[:-1]:
        cells = chunk.split(CELL_MARK)
        # 行尾段落前一格以 \x07 结束，所以最后一段是空的；不是空的说明不是规整的表格行
        if cells[-1].strip(" \r"):
            continue
        cells = cells[:-1]
        if not cells:
            continue
        if "\r" in cells[0]:
            before, _, cells[0] = cells[0].rpartition("\r")
            if before.strip() and rows:
                tables.append(rows)
                rows = []
        row = [_clean(cell) for cell in cells]
        if any(row):
            rows.append(row)
    if rows:
        tables.append(rows)
    return [table for table in tables if len(table) >= 2]
