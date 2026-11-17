"""跨平台、跨 Python 版本的文件变更检测指纹辅助。"""

from __future__ import annotations

import os


def stat_identity(stat: os.stat_result) -> tuple[int, int, int | None, int | None]:
    """提取用于检测读取期间文件变更的稳定指纹（不比较 ctime）。

    Python 3.13 在 Windows 上对同一未修改文件的 ``os.fstat(fd).st_ctime_ns`` 与
    ``os.stat(path).st_ctime_ns`` 会返回不同时间戳（最后写入时间 vs 创建时间），
    因此只比较 ``st_size``、``st_mtime_ns`` 以及可用时的 ``st_dev`` / ``st_ino``。
    """
    dev = getattr(stat, "st_dev", 0) or None
    ino = getattr(stat, "st_ino", 0) or None
    return (stat.st_size, stat.st_mtime_ns, dev, ino)


def same_stat_identity(first: os.stat_result, *others: os.stat_result) -> bool:
    """判断多次 stat 结果是否仍指向同一未修改的文件。"""
    ref_size, ref_mtime, ref_dev, ref_ino = stat_identity(first)
    for item in others:
        size, mtime, dev, ino = stat_identity(item)
        if size != ref_size or mtime != ref_mtime:
            return False
        if ref_dev is not None and dev is not None and dev != ref_dev:
            return False
        if ref_ino is not None and ino is not None and ino != ref_ino:
            return False
    return True
