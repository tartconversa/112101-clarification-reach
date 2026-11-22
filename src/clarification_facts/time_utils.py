"""ISO 8601 带时区时间戳的解析与规整。

仓库内所有时间统一存储为 UTC 的 ``YYYY-MM-DDTHH:MM:SSZ`` 字符串，
因此字典序即时间序，SQL 中可直接比较。
"""

from __future__ import annotations

from datetime import datetime, timezone

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    """当前 UTC 时间（感知时区）。"""
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(value: datetime) -> str:
    """感知时区的 datetime 转为仓库标准 UTC 字符串。"""
    if value.tzinfo is None:
        raise ValueError("datetime 必须带时区信息")
    return value.astimezone(timezone.utc).strftime(UTC_FORMAT)


def now_iso() -> str:
    """当前 UTC 时间的标准字符串。"""
    return to_iso(now_utc())


def parse_iso(value: str) -> datetime:
    """解析 ISO 8601 字符串（接受 ``Z`` 后缀与任意偏移量），返回 UTC datetime。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"时间戳缺少时区信息: {value!r}")
    return dt.astimezone(timezone.utc)


def normalize_iso(value: str) -> str:
    """把任意合法 ISO 8601 带时区字符串规整为仓库标准格式。"""
    return to_iso(parse_iso(value))


def is_iso(value: str) -> bool:
    """是否为可解析的带时区 ISO 8601 字符串。"""
    try:
        parse_iso(value)
    except (ValueError, TypeError):
        return False
    return True
