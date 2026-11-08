"""xlsx、docx（都是 zip 包）交给 openpyxl、python-docx 之前的解压检查。

openpyxl、python-docx 用 ZipFile.read() 一次读完整个部件。只看 zip 目录里写的大小挡不住解压炸弹
（11/5 实测：4.7KB 的 xlsx 让用户上传表格的检查分配 557MB）：
- bzip2、LZMA 部件：Python 每次把读进来的压缩数据整段解开，read(n) 的 n 管不住；
- deflate 部件：目录里把大小写小了，ZipFile.read() 一次能解到 1GB（ZipExtFile.MAX_N），解完才截到
  写的大小；CRC 也照着截短的内容伪造的话，连错都不报，悄悄返回截短的内容。
所以这里只认不压缩和 deflate（Office、WPS、LibreOffice 存的文件只用这两种；11/5 抽查 147 份真实
附件的 3,288 个部件也是），每个部件按 64KB 一块真解一遍、数实际解出多少（不在目录写的大小处停），
合计超过上限就不交给读取库；解出的和目录里写的不一样也不交（文件坏了，或者故意写小）。

通过以后能保证的只是解压出的字节量（按同一份字节、每个部件读一次算）：每个部件读一次最多解出目录
写的大小，所有部件合计不超过 max_bytes；同一部件再读一遍，就再解出同样的量。不保证的：
- 读取库解析以后的对象（openpyxl 的共享字符串、python-docx 的 XML 树等）占多少内存，也不是整个
  进程的内存上限；
- 完整性：检查时不校验 CRC，CRC 错由之后的读取库报；
- 打开 zip 本身的开销：ZipFile() 先解析整个中央目录、检查耗时也和压缩数据量有关，这两样靠各入口
  原有的文件大小上限封顶（上传表格 60KB、名单附件 20MB、结果文件 4MB）。要硬性限内存和时间，
  得把读取放进子进程（另做）。
检查时解压输出按块读（deflate 的 read(n) 每次最多解出 n 字节，块是 64KB），最多比上限多解一块；
压缩数据、目录对象另占内存，由上面说的文件大小上限封顶。
"""

from __future__ import annotations

import copy
import zipfile
import zlib

ZIP_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_CHUNK = 64 * 1024
# zip 规范（APPNOTE 4.4.5）里有的别的压缩方式：文件是好的，只是读取库不支持或 Office 不用。
# 不在这里的编号按文件坏了算（名单解析记坏文件、过后重试，不记"暂不支持"永久不读）
_METHOD_NAMES = {
    1: "shrink",
    6: "implode",
    9: "deflate64",
    zipfile.ZIP_BZIP2: "bzip2",
    zipfile.ZIP_LZMA: "LZMA",
    93: "zstd",
    95: "xz",
    98: "PPMd",
    99: "AES 加密",
}
_KNOWN_METHODS = {2, 3, 4, 5, 10, 16, 18, 19, 20, 94, 96, 97, *_METHOD_NAMES}
# 读部件时可能出的错：文件头坏了、部件重叠（Python 自己查的）、数据截断、deflate 数据坏、
# 专有的加密或补丁格式、文件名解不了码、zip64 里写的偏移大得没法定位（OverflowError）
_READ_ERRORS = (
    zipfile.BadZipFile,
    EOFError,
    zlib.error,
    NotImplementedError,
    RuntimeError,
    ValueError,
    OSError,
    OverflowError,
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
    """所有部件实际解出的合计不超过 max_bytes、部件不超过 max_members 个，才可以交给读取库
    （读取库要读的是同一份字节；保证的范围见模块说明）。"""
    infos = archive.infolist()
    if len(infos) > max_members:
        raise ZipLimitError("members", f"部件太多（{len(infos)} 个）")
    for info in infos:
        if info.compress_type not in ZIP_METHODS:
            if info.compress_type not in _KNOWN_METHODS:
                raise ZipLimitError("broken", f"压缩方式编号不认识（{info.compress_type}）")
            name = _METHOD_NAMES.get(info.compress_type, f"编号 {info.compress_type}")
            raise ZipLimitError("method", f"压缩方式不支持（{name}）", name)
        # 标志位第 0 位加密、第 6 位强加密、第 5 位补丁数据：文件不会变，按不支持（不是坏文件）
        if info.flag_bits & 0x41:
            raise ZipLimitError("method", "压缩方式不支持（加密）", "加密")
        if info.flag_bits & 0x20:
            raise ZipLimitError("method", "压缩方式不支持（补丁数据）", "补丁数据")
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
