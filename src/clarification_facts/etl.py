"""DWD/DWS 重建（可重算的历史口径）。

每次重建：

1. 以一个新的 ``build_id`` 全量重算 DWD 维度/事实与 DWS 汇总；
2. 构建参数（如回执 SLA）写入 ``meta_build.params_json``，口径可复现；
3. 覆盖结果同时追加到 ``dws_coverage_snapshot``，旧快照永不覆盖；
4. ``known_at`` 参数支持"以历史某摄入时点已知的信息"重算（双时间轴）：
   晚于该时点摄入的原始记录一律不参与，得到当时治理人员能看到的口径。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .enums import (
    CoverageBucket,
    ReceiptState,
    SUPERSEDED_CORRECTION_STATES,
)
from .time_utils import now_iso, parse_iso

# 重建时整体清空的表（dim_correction 用 SCD2 逻辑单独处理；
# dws_coverage_snapshot 不在其中：历史快照只追加，永不覆盖）
_REBUILD_TABLES = (
    "dim_content",
    "dim_channel",
    "dim_cohort",
    "fact_publication",
    "fact_delivery_receipt",
    "fact_exposure",
    "fact_audience_membership",
    "fact_cohort_overlap",
    "dws_coverage",
    "dws_exposure_correction",
)


@dataclass
class BuildResult:
    build_id: str
    built_at: str
    known_at: str | None
    receipt_sla_hours: int
    coverage_rows: int
    alignment_rows: int
    snapshot_rows: int


# ---------------------------------------------------------------------------
# DWD 维度
# ---------------------------------------------------------------------------

def _rebuild_dims(conn: sqlite3.Connection, build_id: str,
                  known_at: str | None) -> None:
    cutoff = f"AND ingested_at <= '{known_at}'" if known_at else ""

    conn.execute("DELETE FROM dim_content")
    conn.execute(
        f"""INSERT INTO dim_content (content_id, first_seen_at)
            SELECT content_id, MIN(ingested_at) FROM (
                SELECT content_id, ingested_at FROM raw_correction WHERE 1=1 {cutoff}
                UNION ALL
                SELECT content_id, ingested_at FROM raw_exposure WHERE 1=1 {cutoff}
            ) GROUP BY content_id"""
    )

    conn.execute("DELETE FROM dim_channel")
    conn.execute(
        f"""INSERT INTO dim_channel (channel_id, first_seen_at)
            SELECT channel_id, MIN(ingested_at) FROM (
                SELECT channel_id, ingested_at FROM raw_channel_receipt WHERE 1=1 {cutoff}
                UNION ALL
                SELECT channel_id, ingested_at FROM raw_exposure WHERE 1=1 {cutoff}
            ) GROUP BY channel_id"""
    )

    conn.execute("DELETE FROM dim_cohort")
    conn.execute(
        f"""INSERT INTO dim_cohort (cohort_ref, first_seen_at)
            SELECT cohort_ref, MIN(ingested_at) FROM (
                SELECT cohort_ref, ingested_at FROM raw_channel_receipt WHERE 1=1 {cutoff}
                UNION ALL
                SELECT cohort_ref, ingested_at FROM raw_exposure WHERE 1=1 {cutoff}
                UNION ALL
                SELECT cohort_ref, ingested_at FROM raw_audience_membership WHERE 1=1 {cutoff}
            ) GROUP BY cohort_ref"""
    )

    # SCD2：同一 correction_id 每次摄入的状态变化形成一条历史行
    conn.execute("DELETE FROM dim_correction")
    conn.execute(
        f"""INSERT INTO dim_correction
            (correction_id, content_id, revision, predecessor_id, scope_ref,
             status, confirmed_at, published_at, effective_from, effective_to,
             is_current, build_id, source_record_id)
            SELECT correction_id, content_id, revision, predecessor_id, scope_ref,
                   status, confirmed_at, published_at,
                   ingested_at AS effective_from,
                   LEAD(ingested_at) OVER (
                       PARTITION BY correction_id ORDER BY ingested_at, record_id
                   ) AS effective_to,
                   CASE WHEN ROW_NUMBER() OVER (
                       PARTITION BY correction_id
                       ORDER BY ingested_at DESC, record_id DESC
                   ) = 1 THEN 1 ELSE 0 END AS is_current,
                   ? AS build_id,
                   record_id AS source_record_id
            FROM raw_correction
            WHERE 1=1 {cutoff}""",
        (build_id,),
    )


# ---------------------------------------------------------------------------
# DWD 事实
# ---------------------------------------------------------------------------

def _rebuild_facts(conn: sqlite3.Connection, build_id: str,
                   known_at: str | None) -> None:
    cutoff = f"AND ingested_at <= '{known_at}'" if known_at else ""

    conn.execute("DELETE FROM fact_publication")
    # 发布事实取两个来源的并集：
    #  1) 显式渠道发布记录 raw_channel_publication（运营团队登记的入口生效）；
    #  2) 回执中出现的渠道（向后兼容：无显式发布记录时由回执反推发布）。
    # 发布时间优先取显式记录，其次回落到更正版本的 published_at。
    if known_at:
        pub_sql = """
            INSERT INTO fact_publication
            (correction_id, content_id, channel_id, published_at, build_id)
            SELECT correction_id, content_id, channel_id,
                   MIN(published_at) AS published_at, ?
            FROM (
                SELECT p.correction_id, c.content_id, p.channel_id,
                       p.published_at
                FROM raw_channel_publication p
                JOIN raw_correction c ON c.correction_id = p.correction_id
                WHERE p.ingested_at <= ? AND c.ingested_at <= ?
                UNION ALL
                SELECT r.correction_id, c.content_id, r.channel_id,
                       c.published_at
                FROM raw_channel_receipt r
                JOIN raw_correction c ON c.correction_id = r.correction_id
                WHERE r.ingested_at <= ? AND c.ingested_at <= ?
            ) GROUP BY correction_id, content_id, channel_id"""
        pub_params = (build_id, known_at, known_at, known_at, known_at)
    else:
        pub_sql = """
            INSERT INTO fact_publication
            (correction_id, content_id, channel_id, published_at, build_id)
            SELECT correction_id, content_id, channel_id,
                   MIN(published_at) AS published_at, ?
            FROM (
                SELECT p.correction_id, c.content_id, p.channel_id,
                       p.published_at
                FROM raw_channel_publication p
                JOIN raw_correction c ON c.correction_id = p.correction_id
                UNION ALL
                SELECT r.correction_id, c.content_id, r.channel_id,
                       c.published_at
                FROM raw_channel_receipt r
                JOIN raw_correction c ON c.correction_id = r.correction_id
            ) GROUP BY correction_id, content_id, channel_id"""
        pub_params = (build_id,)
    conn.execute(pub_sql, pub_params)

    conn.execute("DELETE FROM fact_delivery_receipt")
    conn.execute(
        f"""INSERT INTO fact_delivery_receipt
            (record_id, receipt_id, correction_id, channel_id, cohort_ref,
             receipt_state, received_at, ingested_at, is_latest, build_id)
            SELECT record_id, receipt_id, correction_id, channel_id, cohort_ref,
                   receipt_state, received_at, ingested_at,
                   CASE WHEN ROW_NUMBER() OVER (
                       PARTITION BY receipt_id ORDER BY ingested_at DESC, record_id DESC
                   ) = 1 THEN 1 ELSE 0 END,
                   ?
            FROM raw_channel_receipt
            WHERE 1=1 {cutoff}""",
        (build_id,),
    )

    conn.execute("DELETE FROM fact_exposure")
    conn.execute(
        f"""INSERT INTO fact_exposure
            (exposure_id, content_id, channel_id, cohort_ref, started_at,
             ended_at, estimated_views, build_id)
            SELECT exposure_id, content_id, channel_id, cohort_ref, started_at,
                   ended_at, estimated_views, ?
            FROM raw_exposure WHERE 1=1 {cutoff}""",
        (build_id,),
    )

    conn.execute("DELETE FROM fact_audience_membership")
    conn.execute(
        f"""INSERT INTO fact_audience_membership
            (user_id, cohort_ref, channel_id, valid_from, valid_to, build_id)
            SELECT user_id, cohort_ref, channel_id, valid_from, valid_to, ?
            FROM raw_audience_membership WHERE 1=1 {cutoff}""",
        (build_id,),
    )

    conn.execute("DELETE FROM fact_cohort_overlap")
    conn.execute(
        f"""INSERT INTO fact_cohort_overlap
            (cohort_ref_a, cohort_ref_b, channel_a, channel_b, overlap_users, build_id)
            SELECT cohort_ref_a, cohort_ref_b, channel_a, channel_b, overlap_users, ?
            FROM raw_cohort_overlap WHERE 1=1 {cutoff}""",
        (build_id,),
    )


# ---------------------------------------------------------------------------
# DWS：覆盖四态
# ---------------------------------------------------------------------------

def _corrections(conn: sqlite3.Connection, known_at: str | None
                 ) -> list[sqlite3.Row]:
    """每个更正版本取 SCD2 中最新一条状态行（同批次重复记录已去重）。"""
    cutoff = "AND effective_from <= ?" if known_at else ""
    params = [known_at] if known_at else []
    return conn.execute(
        f"""SELECT correction_id, content_id, revision, predecessor_id, scope_ref,
                   status, confirmed_at, published_at,
                   MAX(effective_from) AS latest_effective_from
            FROM dim_correction
            WHERE 1=1 {cutoff}
            GROUP BY correction_id""",
        params,
    ).fetchall()


def _units_for_correction(conn: sqlite3.Connection, corr: sqlite3.Row,
                          known_at: str | None
                          ) -> set[tuple[str, str]]:
    """该更正需要交代的 (渠道, 受众群) 单元全集。

    来源取并集：发布/回执中出现的单元 + 原内容曝光过的单元。
    这样"原内容触达过但更正没有任何动作"的缺口不会被漏掉。
    历史重算（known_at 给定）时只统计该时点之前已摄入的记录。
    """
    receipt_cutoff = "AND ingested_at <= ?" if known_at else ""
    exposure_cutoff = "AND ingested_at <= ?" if known_at else ""
    params: list[object] = [corr["correction_id"]]
    if known_at:
        params.append(known_at)
    params.append(corr["content_id"])
    if known_at:
        params.append(known_at)
    rows = conn.execute(
        f"""SELECT channel_id, cohort_ref FROM raw_channel_receipt
           WHERE correction_id = ? {receipt_cutoff}
           UNION
           SELECT channel_id, cohort_ref FROM raw_exposure
           WHERE content_id = ? {exposure_cutoff}""",
        params,
    ).fetchall()
    return {(r["channel_id"], r["cohort_ref"]) for r in rows}


def _receipts_for_unit(conn: sqlite3.Connection, correction_id: str,
                       channel_id: str, cohort_ref: str,
                       known_at: str | None) -> list[sqlite3.Row]:
    cutoff = "AND ingested_at <= ?" if known_at else ""
    params: list[object] = [correction_id, channel_id, cohort_ref]
    if known_at:
        params.append(known_at)
    return conn.execute(
        f"""SELECT receipt_state, received_at, ingested_at
           FROM fact_delivery_receipt
           WHERE correction_id = ? AND channel_id = ? AND cohort_ref = ?
             AND is_latest = 1 {cutoff}
           ORDER BY received_at""",
        params,
    ).fetchall()


def _publication_for_unit(conn: sqlite3.Connection, correction_id: str,
                          channel_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT MIN(published_at) AS first_published_at
           FROM fact_publication
           WHERE correction_id = ? AND channel_id = ?""",
        (correction_id, channel_id),
    ).fetchone()


def _cohort_size(conn: sqlite3.Connection, channel_id: str, cohort_ref: str
                 ) -> int | None:
    """受众名册规模；无名册返回 None（规模未知，不能当作 0）。"""
    row = conn.execute(
        """SELECT COUNT(DISTINCT user_id) AS n
           FROM fact_audience_membership
           WHERE channel_id = ? AND cohort_ref = ?
             AND (valid_to IS NULL OR valid_to > ?)""",
        (channel_id, cohort_ref, now_iso()),
    ).fetchone()
    if row is None or row["n"] == 0:
        # 也可能是历史名册（全部已失效），仍给已知规模而非 None
        any_row = conn.execute(
            """SELECT COUNT(DISTINCT user_id) AS n
               FROM fact_audience_membership
               WHERE channel_id = ? AND cohort_ref = ?""",
            (channel_id, cohort_ref),
        ).fetchone()
        return any_row["n"] if any_row["n"] > 0 else None
    return row["n"]


def _classify_unit(corr: sqlite3.Row,
                   receipts: list[sqlite3.Row],
                   first_published_at: str | None,
                   target_users: int | None,
                   sla_hours: int) -> tuple[CoverageBucket, str, str | None, int | None]:
    """返回 (覆盖桶, 原因说明, 首次确认时间, 滞后分钟数)。"""
    status = corr["status"]
    confirmed = [r for r in receipts if r["receipt_state"] == ReceiptState.CONFIRMED.value]
    weak = [r for r in receipts if r["receipt_state"] != ReceiptState.CONFIRMED.value]

    # 1) 更正本身已不再适用（撤回 / 被修订替代）
    if status in SUPERSEDED_CORRECTION_STATES:
        reason = f"更正版本状态为 {status}，该版本不再适用"
        if confirmed:
            reason += "（撤回/替代前曾确认到达）"
        return CoverageBucket.NOT_APPLICABLE, reason, (
            confirmed[0]["received_at"] if confirmed else None), None

    # 2) 有确认到达回执
    if confirmed:
        first = confirmed[0]["received_at"]
        lag = int(
            (parse_iso(first) - parse_iso(corr["published_at"])).total_seconds() // 60
        )
        reason = "存在 confirmed 回执，受众到达已确认"
        if weak:
            reason += f"；另有 {len(weak)} 条非确认回执（保留证据）"
        return CoverageBucket.CONFIRMED, reason, first, max(lag, 0)

    # 3) 尚未发布到任何渠道
    if first_published_at is None:
        return (CoverageBucket.NOT_PUBLISHED,
                "更正已确认但该渠道尚无发布事件，无法证明受众收到", None, None)

    # 4) 已发布但无确认回执
    if weak:
        states = ", ".join(sorted({r["receipt_state"] for r in weak}))
        return (CoverageBucket.UNVERIFIABLE,
                f"仅有非确认回执（{states}），到达无法核实；缺失回执不等于零曝光",
                None, None)

    age = parse_iso(now_iso()) - parse_iso(first_published_at)
    if age > timedelta(hours=sla_hours):
        return (CoverageBucket.UNVERIFIABLE,
                f"发布已超过 SLA({sla_hours}h) 仍无任何回执，计为无法核实",
                None, None)
    return (CoverageBucket.PUBLISHED_ONLY,
            "已有发布事件但尚无回执，当前只能证明发布不能证明到达",
            None, None)


def _rebuild_coverage(conn: sqlite3.Connection, build_id: str,
                      sla_hours: int, known_at: str | None = None) -> int:
    conn.execute("DELETE FROM dws_coverage")
    count = 0
    for corr in _corrections(conn, known_at):
        for channel_id, cohort_ref in sorted(
            _units_for_correction(conn, corr, known_at)
        ):
            receipts = _receipts_for_unit(conn, corr["correction_id"],
                                          channel_id, cohort_ref, known_at)
            pub = _publication_for_unit(conn, corr["correction_id"], channel_id)
            first_published_at = pub["first_published_at"] if pub else None
            target_users = _cohort_size(conn, channel_id, cohort_ref)
            bucket, reason, first_confirmed, lag = _classify_unit(
                corr, receipts, first_published_at, target_users, sla_hours
            )
            confirmed_users = (
                target_users
                if bucket is CoverageBucket.CONFIRMED and target_users is not None
                else None
            )
            latest_state = receipts[-1]["receipt_state"] if receipts else None
            conn.execute(
                """INSERT INTO dws_coverage
                   (build_id, correction_id, revision, content_id, scope_ref,
                    channel_id, cohort_ref, coverage_bucket, report_bucket,
                    target_users, confirmed_users, first_published_at,
                    first_confirmed_at, latest_receipt_state, lag_minutes, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (build_id, corr["correction_id"], corr["revision"],
                 corr["content_id"], corr["scope_ref"], channel_id, cohort_ref,
                 bucket.value, bucket.for_report().value, target_users,
                 confirmed_users, first_published_at, first_confirmed,
                 latest_state, lag, reason),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# DWS：曝光与纠正生效对齐（滞后窗口曝光不被抹掉）
# ---------------------------------------------------------------------------

def _first_confirm_for_content_channel_cohort(
    conn: sqlite3.Connection, content_id: str, channel_id: str, cohort_ref: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT c.correction_id, c.published_at,
                  MIN(d.received_at) AS first_confirmed_at
           FROM dim_correction c
           JOIN fact_delivery_receipt d
             ON d.correction_id = c.correction_id
            AND d.channel_id = ?
            AND d.cohort_ref = ?
            AND d.receipt_state = 'confirmed'
            AND d.is_latest = 1
           WHERE c.content_id = ? AND c.is_current = 1
           GROUP BY c.correction_id, c.published_at
           ORDER BY c.published_at
           LIMIT 1""",
        (channel_id, cohort_ref, content_id),
    ).fetchone()


def _rebuild_exposure_alignment(conn: sqlite3.Connection, build_id: str) -> int:
    conn.execute("DELETE FROM dws_exposure_correction")
    exposures = conn.execute(
        """SELECT exposure_id, content_id, channel_id, cohort_ref,
                  started_at, ended_at, estimated_views
           FROM fact_exposure ORDER BY exposure_id"""
    ).fetchall()
    count = 0
    for exp in exposures:
        match = _first_confirm_for_content_channel_cohort(
            conn, exp["content_id"], exp["channel_id"], exp["cohort_ref"]
        )
        correction_id = match["correction_id"] if match else None
        published_at = match["published_at"] if match else None
        confirmed_at = match["first_confirmed_at"] if match else None
        views = exp["estimated_views"]
        start = parse_iso(exp["started_at"])
        end = parse_iso(exp["ended_at"])

        lag_views = post_views = 0
        if confirmed_at is None:
            # 整条曝光都发生在纠正到达之前（或根本没有纠正）：全部计入滞后窗口
            lag_views = views
        else:
            confirm_dt = parse_iso(confirmed_at)
            total = max((end - start).total_seconds(), 1.0)
            lag_secs = max((min(end, confirm_dt) - start).total_seconds(), 0.0)
            post_secs = max((end - max(start, confirm_dt)).total_seconds(), 0.0)
            lag_views = round(views * lag_secs / total)
            post_views = views - lag_views
            # 没有更正发布时间或发布晚于曝光起点时，口径仍以确认时间切分
            if published_at and parse_iso(published_at) > end and lag_views == 0:
                lag_views, post_views = views, 0

        conn.execute(
            """INSERT INTO dws_exposure_correction
               (build_id, exposure_id, content_id, channel_id, cohort_ref,
                exposure_started_at, exposure_ended_at, estimated_views,
                correction_id, correction_published_at, first_confirmed_at,
                lag_window_views, post_coverage_views)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (build_id, exp["exposure_id"], exp["content_id"], exp["channel_id"],
             exp["cohort_ref"], exp["started_at"], exp["ended_at"], views,
             correction_id, published_at, confirmed_at, lag_views, post_views),
        )
        count += 1
    return count


def _snapshot_coverage(conn: sqlite3.Connection, build_id: str) -> int:
    cur = conn.execute(
        """INSERT INTO dws_coverage_snapshot
           (build_id, taken_at, correction_id, channel_id, cohort_ref,
            report_bucket, target_users, confirmed_users, lag_minutes)
           SELECT ?, ?, correction_id, channel_id, cohort_ref, report_bucket,
                  target_users, confirmed_users, lag_minutes
           FROM dws_coverage WHERE build_id = ?""",
        (build_id, now_iso(), build_id),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def rebuild(conn: sqlite3.Connection, *, receipt_sla_hours: int = 72,
            known_at: str | None = None, notes: str | None = None) -> BuildResult:
    """全量重建 DWD/DWS 并登记构建批次。"""
    build_id = f"build_{uuid.uuid4().hex[:12]}"
    built_at = now_iso()
    conn.execute("BEGIN")
    try:
        for table in _REBUILD_TABLES:
            conn.execute(f"DELETE FROM {table}")
        _rebuild_dims(conn, build_id, known_at)
        _rebuild_facts(conn, build_id, known_at)
        coverage_rows = _rebuild_coverage(conn, build_id, receipt_sla_hours,
                                          known_at)
        alignment_rows = _rebuild_exposure_alignment(conn, build_id)
        snapshot_rows = _snapshot_coverage(conn, build_id)

        batch_ids = [
            r["batch_id"]
            for r in conn.execute(
                "SELECT batch_id FROM meta_ingest_batch ORDER BY ingested_at"
            ).fetchall()
            if known_at is None
            or conn.execute(
                "SELECT 1 FROM meta_ingest_batch WHERE batch_id = ? AND ingested_at <= ?",
                (r["batch_id"], known_at),
            ).fetchone()
        ]
        conn.execute(
            """INSERT INTO meta_build
               (build_id, built_at, source_batch_ids, params_json, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (build_id, built_at, json.dumps(batch_ids),
             json.dumps({"receipt_sla_hours": receipt_sla_hours,
                         "known_at": known_at}, ensure_ascii=False),
             notes),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return BuildResult(
        build_id=build_id,
        built_at=built_at,
        known_at=known_at,
        receipt_sla_hours=receipt_sla_hours,
        coverage_rows=coverage_rows,
        alignment_rows=alignment_rows,
        snapshot_rows=snapshot_rows,
    )


def rebuild_at(source_db_path: str | Path, target_db_path: str | Path,
               known_at: str, *, receipt_sla_hours: int = 72) -> BuildResult:
    """在独立的历史副本上按已知摄入时点重算（不影响当前库）。

    复制当前 SQLite 文件后，仅保留 ``ingested_at <= known_at`` 的原始记录与
    批次元数据，再执行常规重建。源库以独立连接只读访问，不影响调用方连接。
    """
    import sqlite3 as _sqlite
    source = Path(source_db_path)
    target = Path(target_db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # WAL 模式下文件拷贝可能漏掉检查点外的页；使用 SQLite 在线备份 API，
    # 保证在其他连接仍打开时得到一致完整的副本。
    src_conn = _sqlite.connect(str(source), isolation_level=None)
    dst_conn = _sqlite.connect(str(target), isolation_level=None)
    try:
        # Connection.backup(target) 把自身内容复制到 target：
        # 源库 -> 目标库，得到含 WAL 未检查点页的一致副本
        src_conn.backup(dst_conn)
    finally:
        src_conn.close()
    hist_conn = dst_conn
    hist_conn.row_factory = sqlite3.Row
    # 副本带来的 DWS/meta_build 属于源库构建，历史库要按已知时点全新构建：
    # 清掉衍生层与构建记录（raw 层仅按 known_at 截断），快照不跨库保留。
    for table in (
        "dim_content", "dim_channel", "dim_cohort", "dim_correction",
        "fact_publication", "fact_delivery_receipt", "fact_exposure",
        "fact_audience_membership", "fact_cohort_overlap",
        "dws_coverage", "dws_exposure_correction", "dws_coverage_snapshot",
        "meta_build",
    ):
        hist_conn.execute(f"DELETE FROM {table}")
    for table in (
        "raw_correction", "raw_channel_publication", "raw_channel_receipt",
        "raw_exposure", "raw_audience_membership", "raw_cohort_overlap",
        "meta_ingest_batch",
    ):
        hist_conn.execute(
            f"DELETE FROM {table} WHERE ingested_at > ?", (known_at,))
    hist_conn.commit()
    try:
        return rebuild(hist_conn, receipt_sla_hours=receipt_sla_hours,
                       known_at=known_at,
                       notes=f"历史时点重算 known_at={known_at}")
    finally:
        hist_conn.close()
