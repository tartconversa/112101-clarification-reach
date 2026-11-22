"""运营查询层：按内容版本、渠道、时间范围解释覆盖与时效。

所有查询基于某次构建（默认最新 ``build_id``），保证报表可追溯到口径批次。
关键纪律：

- 不同渠道的受众**不直接相加**：有名册/重叠表时做去重，没有时只报区间
  （最大值 ~ 求和值），并明确标注"规模未知"。
- 缺失回执的单元计入 ``unverifiable``，绝不按零曝光处理。
- 滞后窗口曝光与确认到达后曝光分列，历史曝光不被新推荐统计抹掉。
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass

from .db import latest_build_id
from .enums import CoverageBucket

_REPORT_ORDER = (
    CoverageBucket.CONFIRMED.value,
    CoverageBucket.PUBLISHED_ONLY.value,
    CoverageBucket.UNVERIFIABLE.value,
    CoverageBucket.NOT_APPLICABLE.value,
)
_REPORT_ZH = {
    "confirmed": "已确认到达",
    "published_only": "仅已发布",
    "unverifiable": "无法核实",
    "not_applicable": "不再适用",
}


def _resolve_build(conn: sqlite3.Connection, build_id: str | None) -> str:
    build_id = build_id or latest_build_id(conn)
    if build_id is None:
        raise RuntimeError("仓库尚未执行过构建，请先运行 rebuild")
    return build_id


# ---------------------------------------------------------------------------
# 四态覆盖
# ---------------------------------------------------------------------------

def coverage_rows(conn: sqlite3.Connection, *,
                  content_id: str | None = None,
                  correction_id: str | None = None,
                  channel_id: str | None = None,
                  cohort_ref: str | None = None,
                  build_id: str | None = None) -> list[sqlite3.Row]:
    """渠道 × 受众 × 更正版本粒度的覆盖证据明细。"""
    build_id = _resolve_build(conn, build_id)
    where = ["build_id = ?"]
    params: list[object] = [build_id]
    for col, val in (
        ("content_id", content_id),
        ("correction_id", correction_id),
        ("channel_id", channel_id),
        ("cohort_ref", cohort_ref),
    ):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    return conn.execute(
        f"""SELECT * FROM dws_coverage
            WHERE {' AND '.join(where)}
            ORDER BY correction_id, channel_id, cohort_ref""",
        params,
    ).fetchall()


def coverage_snapshot(conn: sqlite3.Connection, build_id: str,
                      *, correction_id: str | None = None
                      ) -> list[sqlite3.Row]:
    """读取某历史构建留下的覆盖快照（旧口径不被后续重建覆盖）。"""
    where = ["build_id = ?"]
    params: list[object] = [build_id]
    if correction_id:
        where.append("correction_id = ?")
        params.append(correction_id)
    return conn.execute(
        f"""SELECT * FROM dws_coverage_snapshot
            WHERE {' AND '.join(where)}
            ORDER BY correction_id, channel_id, cohort_ref""",
        params,
    ).fetchall()


def coverage_summary(conn: sqlite3.Connection, *,
                     correction_id: str | None = None,
                     build_id: str | None = None) -> list[sqlite3.Row]:
    """按对外四态汇总单元数与受众规模（target_users 为 NULL 的单元单列）。"""
    build_id = _resolve_build(conn, build_id)
    where = ["build_id = ?"]
    params: list[object] = [build_id]
    if correction_id:
        where.append("correction_id = ?")
        params.append(correction_id)
    return conn.execute(
        f"""SELECT report_bucket,
                   COUNT(*) AS unit_count,
                   SUM(CASE WHEN target_users IS NULL THEN 1 ELSE 0 END)
                       AS units_with_unknown_size,
                   SUM(COALESCE(target_users, 0)) AS known_target_users,
                   SUM(COALESCE(confirmed_users, 0)) AS confirmed_users
            FROM dws_coverage
            WHERE {' AND '.join(where)}
            GROUP BY report_bucket
            ORDER BY CASE report_bucket
                WHEN 'confirmed' THEN 1
                WHEN 'published_only' THEN 2
                WHEN 'unverifiable' THEN 3
                WHEN 'not_applicable' THEN 4 END""",
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# 非相加受众：重叠去重 / 区间估计
# ---------------------------------------------------------------------------

@dataclass
class AudienceReach:
    correction_id: str
    unit_count: int
    summed_targets: int          # 直接相加的结果（可能重复计数，仅上界参考）
    max_unit_target: int         # 单渠道最大单元规模（去重下界）
    deduplicated_targets: int | None  # 有重叠表/名册时的去重值
    unknown_size_units: int
    method: str                  # dedup_overlap | membership | bounded_estimate

    @property
    def display(self) -> str:
        if self.deduplicated_targets is not None:
            return f"{self.deduplicated_targets} 人（去重）"
        if self.unknown_size_units:
            return (f"规模未知单元 {self.unknown_size_units} 个；"
                    f"已知部分区间 {self.max_unit_target}~{self.summed_targets} 人")
        return f"区间 {self.max_unit_target}~{self.summed_targets} 人（无重叠证据，不相加为唯一人数）"


def audience_reach(conn: sqlite3.Connection, correction_id: str, *,
                   build_id: str | None = None) -> AudienceReach:
    """估算一条更正覆盖的去重受众规模，拒绝把渠道受众直接相加。"""
    build_id = _resolve_build(conn, build_id)
    rows = conn.execute(
        """SELECT channel_id, cohort_ref, target_users
           FROM dws_coverage
           WHERE build_id = ? AND correction_id = ?""",
        (build_id, correction_id),
    ).fetchall()
    units = [(r["channel_id"], r["cohort_ref"], r["target_users"]) for r in rows]
    known = [u for u in units if u[2] is not None]
    summed = sum(u[2] for u in known)
    max_unit = max((u[2] for u in known), default=0)
    unknown = sum(1 for u in units if u[2] is None)

    # 优先：用户名册直接去重
    users = {
        r["user_id"]
        for r in conn.execute(
            """SELECT DISTINCT m.user_id
               FROM fact_audience_membership m
               JOIN dws_coverage c
                 ON c.build_id = ? AND c.correction_id = ?
                AND c.channel_id = m.channel_id AND c.cohort_ref = m.cohort_ref""",
            (build_id, correction_id),
        ).fetchall()
    }
    if users:
        return AudienceReach(correction_id, len(units), summed, max_unit,
                             len(users), unknown, "membership")

    # 其次：重叠表做两两扣减（保守口径，仅扣除显式声明的重叠）
    overlap_total = 0
    seen_pairs: set[tuple[str, str, str, str]] = set()
    for (ch_a, co_a, _), (ch_b, co_b, _) in itertools.combinations(known, 2):
        ov = conn.execute(
            """SELECT overlap_users FROM fact_cohort_overlap
               WHERE ((channel_a = ? AND cohort_ref_a = ?)
                      AND (channel_b = ? AND cohort_ref_b = ?))
                  OR ((channel_a = ? AND cohort_ref_a = ?)
                      AND (channel_b = ? AND cohort_ref_b = ?))
               LIMIT 1""",
            (ch_a, co_a, ch_b, co_b, ch_b, co_b, ch_a, co_a),
        ).fetchone()
        if ov and (ch_a, co_a, ch_b, co_b) not in seen_pairs:
            overlap_total += ov["overlap_users"]
            seen_pairs.add((ch_a, co_a, ch_b, co_b))
    if overlap_total:
        return AudienceReach(correction_id, len(units), summed, max_unit,
                             max(summed - overlap_total, max_unit), unknown,
                             "dedup_overlap")

    return AudienceReach(correction_id, len(units), summed, max_unit,
                         None, unknown, "bounded_estimate")


# ---------------------------------------------------------------------------
# 滞后曝光
# ---------------------------------------------------------------------------

def exposure_alignment(conn: sqlite3.Connection, *,
                       content_id: str | None = None,
                       channel_id: str | None = None,
                       start_at: str | None = None,
                       end_at: str | None = None,
                       build_id: str | None = None) -> list[sqlite3.Row]:
    """原曝光 vs 纠正生效/到达的对齐明细（含滞后窗口曝光量）。"""
    build_id = _resolve_build(conn, build_id)
    where = ["build_id = ?"]
    params: list[object] = [build_id]
    for col, val in (("content_id", content_id), ("channel_id", channel_id)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    if start_at is not None:
        where.append("exposure_started_at >= ?")
        params.append(start_at)
    if end_at is not None:
        where.append("exposure_started_at <= ?")
        params.append(end_at)
    return conn.execute(
        f"""SELECT * FROM dws_exposure_correction
            WHERE {' AND '.join(where)}
            ORDER BY exposure_started_at""",
        params,
    ).fetchall()


def lag_totals(conn: sqlite3.Connection, *, build_id: str | None = None
               ) -> sqlite3.Row:
    build_id = _resolve_build(conn, build_id)
    return conn.execute(
        """SELECT COALESCE(SUM(estimated_views), 0) AS total_views,
                  COALESCE(SUM(lag_window_views), 0) AS lag_window_views,
                  COALESCE(SUM(post_coverage_views), 0) AS post_coverage_views,
                  SUM(CASE WHEN correction_id IS NULL THEN 1 ELSE 0 END)
                      AS exposures_without_correction
           FROM dws_exposure_correction WHERE build_id = ?""",
        (build_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# 更正谱系（确认 -> 发布 -> 到达 -> 修订/撤回）
# ---------------------------------------------------------------------------

def correction_lineage(conn: sqlite3.Connection, content_id: str
                       ) -> list[sqlite3.Row]:
    """按内容列出全部更正版本及其状态史（SCD2）与当前覆盖。"""
    return conn.execute(
        """SELECT c.correction_id, c.revision, c.predecessor_id, c.scope_ref,
                  c.status, c.confirmed_at, c.published_at,
                  c.effective_from, c.effective_to, c.is_current,
                  (SELECT COUNT(*) FROM dws_coverage cov
                   WHERE cov.correction_id = c.correction_id
                     AND cov.build_id = (SELECT build_id FROM meta_build
                                         ORDER BY rowid DESC LIMIT 1)
                     AND cov.report_bucket = 'confirmed') AS confirmed_units,
                  (SELECT COUNT(*) FROM dws_coverage cov
                   WHERE cov.correction_id = c.correction_id
                     AND cov.build_id = (SELECT build_id FROM meta_build
                                         ORDER BY rowid DESC LIMIT 1)) AS total_units
           FROM dim_correction c
           WHERE c.content_id = ? AND c.is_current = 1
           ORDER BY c.revision, c.published_at""",
        (content_id,),
    ).fetchall()


def correction_timeline(conn: sqlite3.Connection, correction_id: str
                        ) -> list[dict[str, object]]:
    """把一条更正从确认、发布、回执到修订/撤回的事件按时间合并输出。"""
    events: list[dict[str, object]] = []
    # confirmed/published 是版本固有事件，SCD2 每个历史行会重复出现，按键去重
    seen_lifecycle: set[tuple[str, str]] = set()
    for row in conn.execute(
        "SELECT * FROM dim_correction WHERE correction_id = ? ORDER BY effective_from",
        (correction_id,),
    ).fetchall():
        events.append({
            "event": "correction_state",
            "at": row["effective_from"],
            "detail": f"状态={row['status']} revision={row['revision']}"
                      + ("（当前）" if row["is_current"] else ""),
        })
        if row["confirmed_at"]:
            key = ("confirmed", row["confirmed_at"])
            if key not in seen_lifecycle:
                seen_lifecycle.add(key)
                events.append({"event": "confirmed", "at": row["confirmed_at"],
                               "detail": f"核验确认 scope={row['scope_ref']}"})
        key = ("published", row["published_at"])
        if key not in seen_lifecycle:
            seen_lifecycle.add(key)
            events.append({"event": "published", "at": row["published_at"],
                           "detail": "更正版本发布"})
    for row in conn.execute(
        """SELECT * FROM fact_publication WHERE correction_id = ?
           ORDER BY published_at, channel_id""",
        (correction_id,),
    ).fetchall():
        events.append({"event": "channel_publication",
                       "at": row["published_at"],
                       "detail": f"渠道 {row['channel_id']} 发布"})
    for row in conn.execute(
        """SELECT * FROM fact_delivery_receipt WHERE correction_id = ?
           ORDER BY received_at, receipt_id""",
        (correction_id,),
    ).fetchall():
        events.append({
            "event": "receipt",
            "at": row["received_at"],
            "detail": (f"渠道 {row['channel_id']} / 受众 {row['cohort_ref']} "
                       f"回执={row['receipt_state']}"
                       f"{' [最新]' if row['is_latest'] else ' [旧值]'}"
                       f"（摄入于 {row['ingested_at']}）"),
        })
    return sorted(events, key=lambda e: str(e["at"]))


# ---------------------------------------------------------------------------
# 批次/构建审计
# ---------------------------------------------------------------------------

def ingest_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meta_ingest_batch ORDER BY rowid"
    ).fetchall()


def build_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM meta_build ORDER BY rowid"
    ).fetchall()


def report_bucket_zh(bucket: str) -> str:
    return _REPORT_ZH.get(bucket, bucket)
