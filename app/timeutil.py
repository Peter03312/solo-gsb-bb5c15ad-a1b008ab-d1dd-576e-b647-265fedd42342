"""时间工具：统一按 UTC 处理。

PostgreSQL 的 timestamptz 取回带时区，SQLite 取回为无时区时间；
统一在系统边界归一化，避免 aware/naive 比较报错。
"""
from datetime import datetime, timezone


def ensure_utc(dt: datetime) -> datetime:
    """无时区时间按 UTC 处理；有时区的统一转成 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_ts(dt: datetime) -> str:
    """UTC ISO 8601 规范化时刻，**保留亚秒**（快照 / 表态坐标 / 封存指纹用）。

    整秒时刻格式与既有对外时间格式完全一致：``YYYY-MM-DDTHH:MM:SSZ``
    （不带 ``.000000``，既有快照标识、表态片段坐标与封存摘要逐字节不变）；
    带亚秒的时刻输出 ``YYYY-MM-DDTHH:MM:SS.ffffffZ``（微秒固定六位，不足
    补零），使同一整秒内的不同时刻（如 10:00:01.100 与 10:00:01.900）在
    快照标识、表态片段坐标与封存内容指纹中得到区分。

    数据库以微秒精度保存测温 / 交接时刻（PG timestamptz、SQLite 文本），
    本函数不丢精度；旧记录中已被截为整秒、无法证实亚秒片段仍相同的情形，
    其坐标字符串必然与当前（带小数）坐标不同，由调用方据此判为失效 / 变更，
    绝不按“整秒相等”认定有效或 unchanged。
    """
    dt = ensure_utc(dt)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
