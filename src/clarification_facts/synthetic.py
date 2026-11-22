"""合成场景生成器：构造覆盖全部治理口径的虚构数据，并按摄入批次分目录。

输出到独立根目录（默认 ``data/synthetic/``），与基线 ``sources/`` 物理隔离，
通过 ``extra_source_dirs`` 导入，绝不污染基线资料。

批次目录（每个目录代表一次实际导入，行内 ``ingested_at`` 声明系统摄入时间）：

- ``batch1_2026-04-10/``  首版更正 CR-100、home 确认回执、search partial、
                         原内容早期曝光、受众名册与重叠表。
- ``batch2_2026-04-11/``  search 迟到 confirmed（同 receipt_id 更新）、
                         home 重复回执、修订版 CR-101、新曝光。
- ``batch3_2026-04-13/``  撤回版 CR-102 及其回执、N-101 的更正 CR-200。
- ``batch4_2026-04-15/``  CR-200 的 home 回执（迟到）；push 渠道始终无回执，
                         演示 SLA 缺口与"缺失回执 != 零曝光"。

场景叙事均为虚构，不对应真实个人或平台。
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

from .config import DEFAULT_SYNTH_DIR

HEADER_CORRECTIONS = [
    "record_id", "correction_id", "content_id", "revision", "predecessor_id",
    "scope_ref", "status", "confirmed_at", "published_at", "ingested_at",
]
HEADER_PUBLICATIONS = [
    "record_id", "correction_id", "channel_id", "published_at", "ingested_at",
]
HEADER_RECEIPTS = [
    "record_id", "receipt_id", "correction_id", "channel_id",
    "received_at", "receipt_state", "cohort_ref", "ingested_at",
]
HEADER_EXPOSURES = [
    "record_id", "exposure_id", "content_id", "channel_id",
    "started_at", "ended_at", "cohort_ref", "estimated_views", "ingested_at",
]
HEADER_MEMBERSHIP = [
    "record_id", "user_id", "cohort_ref", "channel_id",
    "valid_from", "valid_to", "ingested_at",
]
HEADER_OVERLAP = [
    "record_id", "cohort_ref_a", "cohort_ref_b", "channel_a",
    "channel_b", "overlap_users", "ingested_at",
]

T1 = "2026-04-10T12:00:00Z"
T2 = "2026-04-11T12:00:00Z"
T3 = "2026-04-13T12:00:00Z"
T4 = "2026-04-15T12:00:00Z"


def _write_csv(path: Path, header: list[str], rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)


def generate_synthetic(out_dir: str | Path = DEFAULT_SYNTH_DIR,
                       *, seed: int = 20260415) -> Path:
    """生成分批次合成 CSV，返回输出根目录。"""
    out = Path(out_dir)
    rng = random.Random(seed)

    b1 = out / "batch1_2026-04-10"
    b2 = out / "batch2_2026-04-11"
    b3 = out / "batch3_2026-04-13"
    b4 = out / "batch4_2026-04-15"

    # --- 更正版本链（按批次出现，形成修订/撤回谱系） ----------------------
    _write_csv(b1 / "corrections.csv", HEADER_CORRECTIONS, [
        ("syn-corr-01", "CR-100", "N-100", 1, "", "CLAIM-42", "partial",
         "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T1),
    ])
    _write_csv(b2 / "corrections.csv", HEADER_CORRECTIONS, [
        # 同一 correction_id 的状态更新：partial -> amended（被新版替代）
        ("syn-corr-01b", "CR-100", "N-100", 1, "", "CLAIM-42", "amended",
         "2026-04-10T08:30:00Z", "2026-04-10T09:00:00Z", T2),
        ("syn-corr-02", "CR-101", "N-100", 2, "CR-100", "CLAIM-42", "published",
         "2026-04-11T10:00:00Z", "2026-04-11T12:00:00Z", T2),
    ])
    _write_csv(b3 / "corrections.csv", HEADER_CORRECTIONS, [
        ("syn-corr-03", "CR-102", "N-100", 3, "CR-101", "CLAIM-42", "withdrawn",
         "2026-04-13T09:00:00Z", "2026-04-13T10:00:00Z", T3),
        ("syn-corr-04", "CR-200", "N-101", 1, "", "CLAIM-88", "published",
         "2026-04-12T14:00:00Z", "2026-04-12T15:00:00Z", T3),
        # CR-300：显式发布到 feed，但回执始终缺失，演示 published_only/SLA 口径
        ("syn-corr-05", "CR-300", "N-102", 1, "", "CLAIM-99", "published",
         "2026-04-13T08:00:00Z", "2026-04-13T09:00:00Z", T3),
    ])

    # --- 显式渠道发布事件（运营登记的入口生效，独立于回执） ---------------
    _write_csv(b1 / "channel_publications.csv", HEADER_PUBLICATIONS, [
        ("syn-pub-01", "CR-100", "home", "2026-04-10T09:00:00Z", T1),
        ("syn-pub-02", "CR-100", "search", "2026-04-10T09:00:00Z", T1),
    ])
    _write_csv(b2 / "channel_publications.csv", HEADER_PUBLICATIONS, [
        ("syn-pub-03", "CR-101", "home", "2026-04-11T12:00:00Z", T2),
        ("syn-pub-04", "CR-101", "search", "2026-04-11T12:00:00Z", T2),
        ("syn-pub-05", "CR-101", "feed", "2026-04-11T12:00:00Z", T2),
    ])
    _write_csv(b3 / "channel_publications.csv", HEADER_PUBLICATIONS, [
        ("syn-pub-06", "CR-102", "home", "2026-04-13T10:00:00Z", T3),
        ("syn-pub-07", "CR-102", "feed", "2026-04-13T10:00:00Z", T3),
        ("syn-pub-08", "CR-200", "home", "2026-04-12T15:00:00Z", T3),
        # CR-200 在 push 上显式发布但永远没有回执 -> SLA 后无法核实
        ("syn-pub-09", "CR-200", "push", "2026-04-12T15:00:00Z", T3),
        ("syn-pub-10", "CR-300", "feed", "2026-04-13T09:00:00Z", T3),
    ])

    # --- 渠道回执（迟到、重复、部分覆盖、永久缺口） -----------------------
    _write_csv(b1 / "channel_receipts.csv", HEADER_RECEIPTS, [
        ("syn-rc-01", "RR-001", "CR-100", "home",
         "2026-04-10T09:20:00Z", "confirmed", "G-H1", T1),
        ("syn-rc-02", "RR-002", "CR-100", "search",
         "2026-04-10T10:05:00Z", "partial", "G-S1", T1),
    ])
    _write_csv(b2 / "channel_receipts.csv", HEADER_RECEIPTS, [
        # 同 receipt_id 的迟到状态更新：partial -> confirmed
        ("syn-rc-03", "RR-002", "CR-100", "search",
         "2026-04-11T08:00:00Z", "confirmed", "G-S1", T2),
        # 不同 record_id 的重复回执（渠道重复推送）
        ("syn-rc-04", "RR-001", "CR-100", "home",
         "2026-04-10T09:25:00Z", "confirmed", "G-H1", T2),
        ("syn-rc-05", "RR-003", "CR-101", "home",
         "2026-04-11T12:20:00Z", "confirmed", "G-H1", T2),
        ("syn-rc-06", "RR-004", "CR-101", "search",
         "2026-04-11T13:00:00Z", "confirmed", "G-S1", T2),
        ("syn-rc-07", "RR-005", "CR-101", "feed",
         "2026-04-11T14:00:00Z", "pending", "G-F1", T2),
    ])
    _write_csv(b3 / "channel_receipts.csv", HEADER_RECEIPTS, [
        # 撤回版本的回执：进入"不再适用"桶，但证据保留
        ("syn-rc-08", "RR-006", "CR-102", "home",
         "2026-04-13T10:20:00Z", "confirmed", "G-H1", T3),
        ("syn-rc-09", "RR-007", "CR-102", "feed",
         "2026-04-13T11:00:00Z", "failed", "G-F1", T3),
    ])
    _write_csv(b4 / "channel_receipts.csv", HEADER_RECEIPTS, [
        # CR-200 的回执迟到两天；push/G-P1 永远没有任何回执
        ("syn-rc-10", "RR-008", "CR-200", "home",
         "2026-04-12T15:20:00Z", "confirmed", "G-H2", T4),
    ])

    # --- 原内容曝光（含纠正到达前的滞后窗口曝光） -------------------------
    exposure_specs = [
        ("N-100", "home", "G-H1", "2026-04-09T08:00:00Z", "2026-04-10T09:20:00Z", b1, T1),
        ("N-100", "search", "G-S1", "2026-04-09T08:00:00Z", "2026-04-11T08:00:00Z", b1, T1),
        ("N-100", "feed", "G-F1", "2026-04-10T00:00:00Z", "2026-04-12T00:00:00Z", b1, T1),
        ("N-100", "home", "G-H1", "2026-04-11T12:20:00Z", "2026-04-12T00:00:00Z", b2, T2),
        ("N-101", "home", "G-H2", "2026-04-11T08:00:00Z", "2026-04-12T15:20:00Z", b3, T3),
        ("N-101", "push", "G-P1", "2026-04-11T08:00:00Z", "2026-04-13T08:00:00Z", b3, T3),
        ("N-102", "feed", "G-F2", "2026-04-12T08:00:00Z", "2026-04-14T08:00:00Z", b3, T3),
    ]
    per_batch: dict[Path, list[tuple[object, ...]]] = {b1: [], b2: [], b3: []}
    for idx, (content, channel, cohort, start, end, batch_dir, stamp) in enumerate(
        exposure_specs, 1
    ):
        per_batch[batch_dir].append(
            (f"syn-exp-{idx:02d}", f"EE-{300 + idx}", content, channel,
             start, end, cohort, rng.randint(80, 600), stamp)
        )
    for batch_dir, rows in per_batch.items():
        if rows:
            _write_csv(batch_dir / "exposures.csv", HEADER_EXPOSURES, rows)

    # --- 受众名册（跨渠道同一用户，演示去重；u12 后续失效演示 SCD 口径） ---
    memberships: list[tuple[object, ...]] = []
    mid = 1

    def add(user: str, cohort: str, channel: str, valid_to: str = "") -> None:
        nonlocal mid
        memberships.append((f"syn-mem-{mid:02d}", user, cohort, channel,
                            "2026-04-01T00:00:00Z", valid_to, T1))
        mid += 1

    for user in ("u01", "u02", "u03", "u04", "u05", "u06"):
        add(user, "G-H1", "home")
    for user in ("u04", "u05", "u06", "u07", "u08", "u09"):
        add(user, "G-S1", "search")
    for user in ("u02", "u10", "u11"):
        add(user, "G-F1", "feed")
    add("u20", "G-F2", "feed")
    for user in ("u10", "u11", "u12"):
        add(user, "G-H2", "home")
    add("u12", "G-P1", "push")
    _write_csv(b1 / "audience_membership.csv", HEADER_MEMBERSHIP, memberships)

    # --- 受众重叠表（渠道单元两两重叠人数） --------------------------------
    _write_csv(b1 / "cohort_overlap.csv", HEADER_OVERLAP, [
        ("syn-ov-01", "G-H1", "G-S1", "home", "search", 3, T1),
        ("syn-ov-02", "G-H1", "G-F1", "home", "feed", 1, T1),
        ("syn-ov-03", "G-H2", "G-P1", "home", "push", 1, T1),
    ])
    return out
