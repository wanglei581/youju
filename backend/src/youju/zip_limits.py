"""xlsx、docx（都是 zip 包）交给 openpyxl、python-docx 之前的解压检查。

openpyxl、python-docx 用 ZipFile.read() 一次读完整个部件。只看 zip 目录里写的大小挡不住解压炸弹
（11/5 实测：4.7KB 的 xlsx 让用户上传表格的检查分配 557MB）：
- bzip2、LZMA 部件：Python 每次把读进来的压缩数据整段解开，read(n) 的 n 管不住；
- deflate 部件：目录里把大小写小了，ZipFile.read() 一次能解到 1GB（ZipExtFile.MAX_N），解完才截到
  写的大小；CRC 也照着截短的内容伪造的话，连错都不报，悄悄返回截短的内容。
所以这里只认不压缩和 deflate（Office、WPS、LibreOffice 存的文件只用这两种；11/5 抽查 147 份真实
附件的 3,288 个部件也是），每个部件按 64KB 一块真解一遍、数实际解出多少（不在目录写的大小处停），
合计超过上限就不交给读取库；解出的和目录里写的不一样也不交（文件坏了，或者故意写小）。
检查本身内存只占一块（deflate 的 read(n) 每次最多解出 n 字节），最多解到上限多一个字节。
"""

from __future__ import annotations

import copy
import zipfile
import zlib

ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_CHUNK = 64 * 1024
_METHOD_NAMES = {zipfile.ZIP_BZIP2: "bzip2", zipfile.ZIP_LZMA: "LZMA"}
# 读部件时可能出的错：文件头坏了、部件重叠（Python 自己查的）、数据截断、deflate 数据坏、
# 专有的加密或补丁格式、文件名解不了码
_READ_ERRORS = (
    zipfile.BadZipFile,
    EOFError,
    zlib.error,
    NotImplementedError,
    RuntimeError,
    ValueError,
    OSError,
)


class ZipLimitError(ValueError):
    """不能交给读取库的 zip 包。kind 是 method（压缩方式不支持或加了密，detail 写是哪种）、
    members（部件太多）、size（解压后太大）、broken（解出的大小和目录不符、部件重叠、数据坏了），
    调用方按它给自己的说法。"""

    def __init__(self, kind: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def check(archive: zipfile.ZipFile, *, max_bytes: int, max_members: int) -> None:
    """所有部件实际解出的合计不超过 max_bytes、部件不超过 max_members 个，才可以交给读取库。"""
    infos = archive.infolist()
    if len(infos) > max_members:
        raise ZipLimitError("members", f"部件太多（{len(infos)} 个）")
    for info in infos:
        if info.compress_type not in ZIP_METHODS:
            name = _METHOD_NAMES.get(info.compress_type, f"编号 {info.compress_type}")
            raise ZipLimitError("method", f"压缩方式不支持（{name}）", name)
        if info.flag_bits & 0x1:
            raise ZipLimitError("method", "压缩方式不支持（加密）", "加密")
    # 两个目录项指向同一段数据：Python 3.12 只警告不拦，同一段会被当成两个部件各解一遍
    if len({info.header_offset for info in infos}) < len(infos):
        raise ZipLimitError("broken", "部件重叠")
    # 先按目录写的大小挡（正常文件写的都准，不用解就能拒），再真解一遍数
    if sum(info.file_size for info in infos) > max_bytes:
        raise ZipLimitError("size", "解压后过大")
    total = 0
    for info in infos:
        size = _inflated_size(archive, info, max_bytes - total)
        total += size
        if total > max_bytes:
            raise ZipLimitError("size", "解压后过大")
        if size != info.file_size:
            raise ZipLimitError("broken", f"部件实际大小和目录不符：{info.filename[:80]}")


def _inflated_size(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> int:
    """这个部件实际能解出多少字节；超过 limit 时数到 limit + 1 就停。

    读的是目录项的一份副本：大小改成 limit + 1（zipfile 读到目录写的大小就停，不改数不出写小的
    那部分）；去掉 CRC（数到一半停下时不校验，CRC 由之后真正读的库按原目录项校验）。"""
    probe = copy.copy(info)
    probe.file_size = limit + 1
    del probe.CRC
    size = 0
    try:
        with archive.open(probe) as fh:
            while chunk := fh.read(_CHUNK):
                size += len(chunk)
    except _READ_ERRORS as exc:
        raise ZipLimitError("broken", f"部件读不了：{info.filename[:80]}") from exc
    return size
