"""来源行级校验。

校验失败抛出 :class:`ValidationError`，摄入批次据此拒绝整个文件
（原始层不写入半合法数据）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .enums import CorrectionStatus, ReceiptState
from .time_utils import is_iso, parse_iso


class ValidationError(ValueError):
    """来源数据不满足仓库契约。"""


REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "corrections": (
        "record_id", "correction_id", "content_id", "revision",
        "published_at", "scope_ref", "status",
    ),
    "channel_publications": (
        "record_id", "correction_id", "channel_id", "published_at",
    ),
    "channel_receipts": (
        "record_id", "receipt_id", "correction_id", "channel_id",
        "received_at", "receipt_state", "cohort_ref",
    ),
    "exposures": (
        "record_id", "exposure_id", "content_id", "channel_id",
        "started_at", "ended_at", "cohort_ref", "estimated_views",
    ),
    "audience_membership": (
        "record_id", "user_id", "cohort_ref", "channel_id",
        "valid_from", "valid_to",
    ),
    "cohort_overlap": (
        "record_id", "cohort_ref_a", "cohort_ref_b", "channel_a",
        "channel_b", "overlap_users",
    ),
}


@dataclass(frozen=True)
class RowError:
    source_kind: str
    record_id: str
    line_no: int
    message: str

    def __str__(self) -> str:
        return f"{self.source_kind} 第 {self.line_no} 行({self.record_id}): {self.message}"


def _require_non_empty(row: dict[str, str], col: str, kind: str, line_no: int) -> str:
    value = (row.get(col) or "").strip()
    if not value:
        raise ValidationError(f"{kind} 第 {line_no} 行: 字段 {col} 不能为空")
    return value


def _parse_int(row: dict[str, str], col: str, kind: str, line_no: int) -> int:
    raw = _require_non_empty(row, col, kind, line_no)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(
            f"{kind} 第 {line_no} 行: 字段 {col} 不是整数: {raw!r}"
        ) from exc
    if value < 0:
        raise ValidationError(
            f"{kind} 第 {line_no} 行: 字段 {col} 不能为负数"
        )
    return value


def _parse_ts(row: dict[str, str], col: str, kind: str, line_no: int,
              required: bool = True) -> str | None:
    raw = (row.get(col) or "").strip()
    if not raw:
        if required:
            raise ValidationError(f"{kind} 第 {line_no} 行: 字段 {col} 不能为空")
        return None
    if not is_iso(raw):
        raise ValidationError(
            f"{kind} 第 {line_no} 行: 字段 {col} 不是带时区的 ISO 8601 时间: {raw!r}"
        )
    # 规整为标准 UTC 形式
    from .time_utils import normalize_iso
    return normalize_iso(raw)


def validate_header(kind: str, fieldnames: list[str] | None) -> None:
    if not fieldnames:
        raise ValidationError(f"{kind}: 缺少表头")
    missing = [c for c in REQUIRED_COLUMNS[kind] if c not in fieldnames]
    if missing:
        raise ValidationError(f"{kind}: 缺少必需列 {missing}，实际列为 {fieldnames}")


def validate_row(kind: str, row: dict[str, str], line_no: int) -> dict[str, str | int | None]:
    """校验并返回规整后的行（时间转为标准 UTC，数字转为 int）。"""
    record_id = _require_non_empty(row, "record_id", kind, line_no)

    if kind == "corrections":
        out: dict[str, str | int | None] = {
            "record_id": record_id,
            "correction_id": _require_non_empty(row, "correction_id", kind, line_no),
            "content_id": _require_non_empty(row, "content_id", kind, line_no),
            "revision": _parse_int(row, "revision", kind, line_no),
            "predecessor_id": (row.get("predecessor_id") or "").strip() or None,
            "scope_ref": _require_non_empty(row, "scope_ref", kind, line_no),
            "status": _require_non_empty(row, "status", kind, line_no),
            "confirmed_at": _parse_ts(row, "confirmed_at", kind, line_no, required=False),
            "published_at": _parse_ts(row, "published_at", kind, line_no),
        }
        if out["status"] not in {s.value for s in CorrectionStatus}:
            raise ValidationError(
                f"{kind} 第 {line_no} 行: 未知更正状态 {out['status']!r}"
            )
        if out["revision"] < 1:
            raise ValidationError(f"{kind} 第 {line_no} 行: revision 必须 >= 1")
        return out

    if kind == "channel_publications":
        return {
            "record_id": record_id,
            "correction_id": _require_non_empty(row, "correction_id", kind, line_no),
            "channel_id": _require_non_empty(row, "channel_id", kind, line_no),
            "published_at": _parse_ts(row, "published_at", kind, line_no),
        }

    if kind == "channel_receipts":
        out = {
            "record_id": record_id,
            "receipt_id": _require_non_empty(row, "receipt_id", kind, line_no),
            "correction_id": _require_non_empty(row, "correction_id", kind, line_no),
            "channel_id": _require_non_empty(row, "channel_id", kind, line_no),
            "receipt_state": _require_non_empty(row, "receipt_state", kind, line_no),
            "cohort_ref": _require_non_empty(row, "cohort_ref", kind, line_no),
            "received_at": _parse_ts(row, "received_at", kind, line_no),
        }
        if out["receipt_state"] not in {s.value for s in ReceiptState}:
            raise ValidationError(
                f"{kind} 第 {line_no} 行: 未知回执状态 {out['receipt_state']!r}"
            )
        return out

    if kind == "exposures":
        out = {
            "record_id": record_id,
            "exposure_id": _require_non_empty(row, "exposure_id", kind, line_no),
            "content_id": _require_non_empty(row, "content_id", kind, line_no),
            "channel_id": _require_non_empty(row, "channel_id", kind, line_no),
            "cohort_ref": _require_non_empty(row, "cohort_ref", kind, line_no),
            "started_at": _parse_ts(row, "started_at", kind, line_no),
            "ended_at": _parse_ts(row, "ended_at", kind, line_no),
            "estimated_views": _parse_int(row, "estimated_views", kind, line_no),
        }
        if parse_iso(str(out["ended_at"])) < parse_iso(str(out["started_at"])):
            raise ValidationError(
                f"{kind} 第 {line_no} 行: ended_at 早于 started_at"
            )
        return out

    if kind == "audience_membership":
        valid_from = _parse_ts(row, "valid_from", kind, line_no)
        valid_to = _parse_ts(row, "valid_to", kind, line_no, required=False)
        if valid_to is not None and parse_iso(str(valid_to)) < parse_iso(str(valid_from)):
            raise ValidationError(
                f"{kind} 第 {line_no} 行: valid_to 早于 valid_from"
            )
        return {
            "record_id": record_id,
            "user_id": _require_non_empty(row, "user_id", kind, line_no),
            "cohort_ref": _require_non_empty(row, "cohort_ref", kind, line_no),
            "channel_id": _require_non_empty(row, "channel_id", kind, line_no),
            "valid_from": valid_from,
            "valid_to": valid_to,
        }

    if kind == "cohort_overlap":
        a = _require_non_empty(row, "cohort_ref_a", kind, line_no)
        b = _require_non_empty(row, "cohort_ref_b", kind, line_no)
        if a == b:
            raise ValidationError(
                f"{kind} 第 {line_no} 行: 重叠记录的两个 cohort 不能相同"
            )
        return {
            "record_id": record_id,
            "cohort_ref_a": a,
            "cohort_ref_b": b,
            "channel_a": _require_non_empty(row, "channel_a", kind, line_no),
            "channel_b": _require_non_empty(row, "channel_b", kind, line_no),
            "overlap_users": _parse_int(row, "overlap_users", kind, line_no),
        }

    raise ValidationError(f"未知来源类型: {kind}")
