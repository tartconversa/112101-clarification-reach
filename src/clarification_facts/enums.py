"""领域枚举与状态口径。

核心口径（治理问题的答案分桶）：

- ``CONFIRMED``          已确认到达：存在覆盖该渠道/受众的 confirmed 回执。
- ``PUBLISHED_ONLY``     仅已发布：渠道发布事件存在，但没有可确认到达的回执。
- ``UNVERIFIABLE``       无法核实：有回执线索但状态为 partial/pending/failed，
                         或超过 SLA 仍无任何回执（缺失回执不等于零曝光）。
- ``NOT_APPLICABLE``     不再适用：更正已被撤回（withdrawn）或被后续修订替代，
                         该版本不再需要继续追踪到达。
- ``NOT_PUBLISHED``      仓库内部状态：更正已确认但尚无任何渠道发布事件，
                         对外报表中并入 UNVERIFIABLE 并附说明。
"""

from __future__ import annotations

from enum import Enum


class CorrectionStatus(str, Enum):
    """核验团队给出的更正版本生命周期状态。"""

    PUBLISHED = "published"   # 已确认并发布的有效更正
    AMENDED = "amended"       # 已被后续修订替代
    PARTIAL = "partial"       # 部分更正（仅纠正原说法的一部分）
    WITHDRAWN = "withdrawn"   # 撤回，原更正不再适用


class ReceiptState(str, Enum):
    """渠道回执状态（回执可能迟到、重复、只覆盖部分受众）。"""

    CONFIRMED = "confirmed"   # 已确认到达
    PARTIAL = "partial"       # 仅覆盖部分受众
    PENDING = "pending"       # 渠道已受理，到达结果待返回
    FAILED = "failed"         # 渠道明确返回投递失败
    EXPIRED = "expired"       # 超出可回执窗口，永远无法核实


class CoverageBucket(str, Enum):
    """治理口径四态（加内部 NOT_PUBLISHED）。"""

    CONFIRMED = "confirmed"
    PUBLISHED_ONLY = "published_only"
    UNVERIFIABLE = "unverifiable"
    NOT_APPLICABLE = "not_applicable"
    NOT_PUBLISHED = "not_published"

    @property
    def zh_label(self) -> str:
        return _BUCKET_ZH[self]

    def for_report(self) -> "CoverageBucket":
        """对外报表口径：NOT_PUBLISHED 并入 UNVERIFIABLE。"""
        if self is CoverageBucket.NOT_PUBLISHED:
            return CoverageBucket.UNVERIFIABLE
        return self


_BUCKET_ZH = {
    CoverageBucket.CONFIRMED: "已确认到达",
    CoverageBucket.PUBLISHED_ONLY: "仅已发布",
    CoverageBucket.UNVERIFIABLE: "无法核实",
    CoverageBucket.NOT_APPLICABLE: "不再适用",
    CoverageBucket.NOT_PUBLISHED: "尚未发布",
}


# 回执状态 -> 覆盖桶的映射（还需结合发布事件与更正生命周期，见 metrics 模块）
RECEIPT_STATE_TO_BUCKET = {
    ReceiptState.CONFIRMED.value: CoverageBucket.CONFIRMED,
    ReceiptState.PARTIAL.value: CoverageBucket.UNVERIFIABLE,
    ReceiptState.PENDING.value: CoverageBucket.UNVERIFIABLE,
    ReceiptState.FAILED.value: CoverageBucket.UNVERIFIABLE,
    ReceiptState.EXPIRED.value: CoverageBucket.UNVERIFIABLE,
}

# 终态回执：不会再被迟到的同键回执改变
TERMINAL_RECEIPT_STATES = frozenset(
    {ReceiptState.CONFIRMED.value, ReceiptState.EXPIRED.value}
)

# 视为"仍然有效、需要追踪到达"的更正状态
ACTIVE_CORRECTION_STATES = frozenset(
    {CorrectionStatus.PUBLISHED.value, CorrectionStatus.PARTIAL.value}
)

# 不再适用的更正状态
SUPERSEDED_CORRECTION_STATES = frozenset(
    {CorrectionStatus.AMENDED.value, CorrectionStatus.WITHDRAWN.value}
)
