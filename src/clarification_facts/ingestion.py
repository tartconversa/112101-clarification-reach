"""原始层摄入：CSV -> 仅追加 raw_ 表，带批次与双时间轴。

特性：

- 幂等：同 ``record_id`` 且载荷一致 -> 跳过并计 duplicated；
  载荷变化 -> strict 模式拒绝（原始证据不可被静默改写）。
- 双时间轴：``*_at`` 为业务事件时间，``ingested_at`` 为系统摄入时间，
  两者独立，支撑迟到/重复回执与历史口径重算。
- 整文件事务：任一行校验失败则该文件整体回滚，不产生半成品批次。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import WarehouseConfig
from .db import connect, init_schema
from .time_utils import is_iso, now_iso
from .validation import ValidationError, validate_header, validate_row

# 来源类型 -> (raw 表, 列顺序)
_RAW_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "corrections": (
        "raw_correction",
        ("correction_id", "content_id", "revision", "predecessor_id",
         "scope_ref", "status", "confirmed_at", "published_at"),
    ),
    "channel_publications": (
        "raw_channel_publication",
        ("correction_id", "channel_id", "published_at"),
    ),
    "channel_receipts": (
        "raw_channel_receipt",
        ("receipt_id", "correction_id", "channel_id", "receipt_state",
         "cohort_ref", "received_at"),
    ),
    "exposures": (
        "raw_exposure",
        ("exposure_id", "content_id", "channel_id", "cohort_ref",
         "started_at", "ended_at", "estimated_views"),
    ),
    "audience_membership": (
        "raw_audience_membership",
        ("user_id", "cohort_ref", "channel_id", "valid_from", "valid_to"),
    ),
    "cohort_overlap": (
        "raw_cohort_overlap",
        ("cohort_ref_a", "cohort_ref_b", "channel_a", "channel_b", "overlap_users"),
    ),
}


@dataclass
class IngestResult:
    batch_id: str
    source_kind: str
    source_file: str
    rows_seen: int
    rows_inserted: int
    rows_duplicated: int
    rows_rejected: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "batch_id": self.batch_id,
            "source_kind": self.source_kind,
            "source_file": self.source_file,
            "rows_seen": self.rows_seen,
            "rows_inserted": self.rows_inserted,
            "rows_duplicated": self.rows_duplicated,
            "rows_rejected": self.rows_rejected,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(path: Path, kind: str) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        validate_header(kind, reader.fieldnames)
        return list(reader)


def ingest_file(
    conn: sqlite3.Connection,
    kind: str,
    path: str | Path,
    *,
    strict: bool = True,
    note: str | None = None,
    ingested_at: str | None = None,
) -> IngestResult:
    """把单个 CSV 文件摄入对应 raw 表（整文件事务）。"""
    if kind not in _RAW_TABLES:
        raise ValidationError(f"未知来源类型: {kind}")
    path = Path(path)
    table, data_cols = _RAW_TABLES[kind]
    batch_id = f"ing_{uuid.uuid4().hex[:12]}"
    stamp = ingested_at or now_iso()

    raw_rows = _read_rows(path, kind)  # 表头/结构问题在事务外先暴露
    # 可选的 ingested_at 列：允许来源显式声明摄入时间，构造迟到/分批次场景；
    # 缺省时整批使用调用时刻（或调用方给定的 ingested_at）。
    cleaned: list[tuple[dict[str, str | int | None], str | None]] = []
    for offset, raw in enumerate(raw_rows, start=2):  # CSV 第 1 行是表头
        row_ingested = (raw.get("ingested_at") or "").strip() or None
        if row_ingested is not None and not is_iso(row_ingested):
            raise ValidationError(
                f"{kind} 第 {offset} 行: ingested_at 不是带时区 ISO 8601 时间: {row_ingested!r}"
            )
        from .time_utils import normalize_iso
        cleaned.append((
            validate_row(kind, raw, offset),
            normalize_iso(row_ingested) if row_ingested else None,
        ))

    inserted = duplicated = rejected = 0
    try:
        conn.execute("BEGIN")
        for clean, row_ingested_at in cleaned:
            payload = json.dumps(clean, ensure_ascii=False, sort_keys=True)
            existing = conn.execute(
                f"SELECT payload_json FROM {table} WHERE record_id = ?",
                (clean["record_id"],),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] == payload:
                    duplicated += 1
                    continue
                rejected += 1
                if strict:
                    raise ValidationError(
                        f"record_id={clean['record_id']} 已存在且载荷发生变化；"
                        "原始记录不可变，strict 模式拒绝导入。"
                    )
                continue
            row_stamp = row_ingested_at or stamp
            cols = ("record_id", *data_cols, "payload_json", "batch_id", "ingested_at")
            params: list[object] = [clean["record_id"]]
            params.extend(clean[c] for c in data_cols)
            params.extend([payload, batch_id, row_stamp])
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                params,
            )
            inserted += 1

        conn.execute(
            """INSERT INTO meta_ingest_batch
               (batch_id, source_kind, source_file, file_sha256, ingested_at,
                rows_seen, rows_inserted, rows_duplicated, rows_rejected, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, kind, str(path), _file_sha256(path), stamp,
             len(cleaned), inserted, duplicated, rejected, note),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return IngestResult(
        batch_id=batch_id,
        source_kind=kind,
        source_file=str(path),
        rows_seen=len(cleaned),
        rows_inserted=inserted,
        rows_duplicated=duplicated,
        rows_rejected=rejected,
    )


def ingest_config_sources(
    config: WarehouseConfig,
    *,
    note: str | None = None,
) -> list[IngestResult]:
    """按配置摄入全部已知来源文件；缺失的扩展表自动跳过。"""
    results: list[IngestResult] = []
    conn = connect(config.db_path)
    try:
        init_schema(conn)
        for kind, paths in config.candidate_files().items():
            for path in paths:
                results.append(
                    ingest_file(conn, kind, path, strict=config.strict, note=note)
                )
    finally:
        conn.close()
    return results
