# 验收与 eval 设计（已提升，正文已迁出）

> ⚠️ 本文中的 `Dxx` 是 **2026-09-21 架构重构前的旧编号**，与现行 `DECISIONS.md` 不对应。对照表见 `DECISIONS.md`「附录 · 旧编号对照」。

> **文档契约** · 类型：归档 · 读取：**不读**。本文已无正文，按下表直接去新位置
> 更新：冻结。不再接受任何内容写入
> 独占：无
> 不收录：一切正文。原内容已按 `DECISIONS.md` D15 迁至下表各处

本文原为验收用例设计初稿（2026-09-09）。2026-09-14 正文已全部迁出并成为正式验收章节，此处只留去向对照。**完整原文见 Git 历史**（`git show 5eb4dbc:data/supplychain/ACCEPTANCE-EVAL.md`）。

| 原章节 | 现正文位置 |
|---|---|
| §1 判定原则 | `docs/product/acceptance-cases.md` §1 判定原则 |
| §2 案例清单 EV-01～EV-28 | `docs/product/acceptance-cases.md` §2 用例清单，按验证层级分组 |
| §3 执行层次与门槛 | 四层定义 → `DECISIONS.md` D14；硬门禁 → `docs/product/acceptance-cases.md` §1 |
| §4 需求追踪索引 | `docs/product/acceptance-cases.md` §3 FR → EV 追踪矩阵 |

两处已知的过时数据在迁移时已订正：原文「当前 162 条记录」是换数据集前的旧值，现为 121 条用料行 / 148 个候选；原文提及的「28 例」实为 26 例，EV-15／EV-16 已随**旧** D12 删除，编号不回收（编号治理见 `CODING_RULES.md`「编号治理」）。旧编号与 2026-09-21 重排后的新 D01–D19 不对应，对照见 `DECISIONS.md` 末尾的旧编号对照表。
