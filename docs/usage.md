# 口径与架构说明

## 1. 角色与职责边界

| 团队 | 维护的数据 | 回答的问题 |
| --- | --- | --- |
| 核验团队 | 更正版本（correction）：原说法 `scope_ref`、修订号、状态、确认时间、前驱版本 | 原说法是什么、更正成什么、适用于哪个版本 |
| 分发运营团队 | 渠道发布、到达回执、原内容曝光、受众名册/重叠 | 更正在哪些入口生效、到达了谁、何时完成、滞后多久 |

发布回执只证明"更正被发布到渠道"，不证明任何受众收到。仓库在模型上把
`fact_publication`（发布）与 `fact_delivery_receipt`（到达）严格分开。

## 2. 更正版本生命周期

```
published/partial  ──修订到达──▶  amended（旧版本不再适用，证据保留）
       │
       └──撤回──▶ withdrawn（不再适用）
```

- `partial`：部分更正，仍然有效，需要追踪到达。
- `amended`：被后续 revision 替代；同一 `correction_id` 的状态变化以 SCD2 保留，
  新版本通过 `predecessor_id` 链接成修订链。
- `withdrawn`：撤回；撤回前产生的回执仍保留可查。

`dim_correction` 对同一 `correction_id` 的每次摄入状态变化生成一行
（`effective_from/effective_to/is_current`），完整状态史不被覆盖。

## 3. 覆盖四态判定顺序

对每个（更正版本 × 渠道 × 受众群）单元：

1. 更正状态为 amended/withdrawn → `not_applicable`（若曾有 confirmed，原因中注明）。
2. 存在 `confirmed` 回执（取同 `receipt_id` 最新一版）→ `confirmed`，
   并计算发布到首次确认的 `lag_minutes`。
3. 该渠道无发布事件 → 内部 `not_published`，对外并入 `unverifiable`。
4. 仅有 partial/pending/failed/expired 回执 → `unverifiable`。
5. 无任何回执：未超 SLA → `published_only`；超 SLA → `unverifiable`。

其中"是否已发布"依据 `fact_publication`，它取两个来源的并集：
显式 `channel_publications` 记录（优先）与回执反推（兼容）。
因此"已发布但无回执"与"根本没发布到该渠道"是可区分的两种状态。

单元全集取"回执中出现的单元"与"原内容曝光过的单元"的并集，
保证原内容触达过但更正毫无动作的缺口不会被漏报。

## 4. 回执的迟到与重复

- 同一 `receipt_id` 多次出现：全部保留，窗口函数按 `ingested_at, record_id`
  标记 `is_latest`，判定只依据最新一版，旧值在时间线中仍可见。
- 不同 `record_id` 的重复推送：作为独立原始记录保留。
- 回执事件时间（`received_at`）与摄入时间（`ingested_at`）独立存储，
  迟到回执可在双时间轴重放中被准确排除/纳入。

## 5. 受众非相加口径

`audience_reach` 的三级证据策略：

1. `membership`：存在受众名册 → 按 `user_id` 集合去重，给出唯一人数。
2. `dedup_overlap`：无名册但有 `cohort_overlap` → 求和后扣除显式重叠
   （保守下界，不低于最大单元规模）。
3. `bounded_estimate`：两者皆无 → 只报告区间
   `[最大单元规模, 各单元求和]` 与规模未知单元数，不产生唯一人数。

`target_users` 为 NULL 表示规模未知，参与任何求和时按未知处理而非 0。

## 6. 滞后窗口曝光

对每条原曝光，按适用更正的**首次 confirmed 到达时间**切分：

- 曝光区间在确认时间之前的部分 → `lag_window_views`（按秒比例分摊估计量）。
- 确认时间之后的部分 → `post_coverage_views`。
- 无任何更正/确认的曝光 → 整条计入滞后窗口，并计入
  `exposures_without_correction`。

`lag_totals` 给出全局合计：历史曝光总量、滞后窗口量、确认后量三者可对账，
新的推荐数据不会修改或抵消历史行。

## 7. 可重算性与双时间轴

- 原始层仅追加：任何重建都从 `raw_*` 全量重算 DWD/DWS，结果确定。
- 每次构建登记 `meta_build`：纳入的批次 ID 与参数（SLA、known_at）写入 JSON，
  口径可复现。
- `dws_coverage_snapshot` 只追加：旧构建的覆盖结论永久保留，
  即使 `dws_coverage` 当前表已被新构建替换。
- `rebuild_at(known_at)`：用 SQLite 在线 backup 复制数据库，
  删除晚于 known_at 摄入的原始记录后重建，得到该历史时点的治理视角，
  源库不受影响。

## 8. 已知边界

- 曝光在时间窗内按秒均摊估计滞后比例；若来源提供分段曝光量应改为直接读入。
- `dedup_overlap` 只做两两扣减，多单元高阶重叠未建模（名册存在时优先走精确去重）。
- 受众名册的 SCD（`valid_from/valid_to`）已入库；当前规模查询按当前有效行，
  历史时点规模可结合 `known_at` 扩展。
