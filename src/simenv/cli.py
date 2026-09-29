"""文件摘要（F03 环境内核 · 命令行）

`make sim-run SEED=42`：不接 Agent，按种子生成环境并跑满全程，逐日打印有事发生的日子，最后打印结果指标 JSON。
这是"什么都不做"基线的雏形（正式基线归 F04）；同一种子两次输出逐字相同，可直接 diff 验证复现。
依赖：params、bom、api.Environment、metrics。纯内存运行，不连数据库、不联网。
"""
from __future__ import annotations  # 类型注解延迟求值

import argparse  # 命令行参数
import json  # 输出指标

from simenv.api import Environment  # 环境门面
from simenv.bom import load_boms  # 原理图 BOM
from simenv.metrics import outcome_metrics  # 结果指标
from simenv.params import DEFAULT_PARAMS, load_params  # 参数


# ---------------------------------------------------------------------------
# main：命令行入口
# 功能：解析种子与参数文件，跑完全程，打印逐日摘要与指标。
# 依赖：见文件摘要；被 Makefile 的 sim-run 目标调用。
# 完成：只打印有事件的日子，停线只在开始那天打印一次，避免满屏重复。
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="无 Agent 跑完模拟环境并打印结果指标")  # 参数解析器
    parser.add_argument("--seed", type=int, required=True)  # 随机种子
    parser.add_argument("--params", default=str(DEFAULT_PARAMS))  # 参数文件
    args = parser.parse_args()  # 解析
    env = Environment.create(load_params(args.params), load_boms(), args.seed)  # 生成环境（纯内存）
    print(f"seed={args.seed} orders={len(env.list_orders())} "  # 开局概况
          f"work_orders={len(env.list_work_orders())} "  # 工单数
          f"in_transit={len(env.list_shipments(status='in_transit'))}")  # 在途运单数
    stopped_before: set[str] = set()  # 前一天处于停线的工单
    while env.state.day < env.state.horizon_days:  # 跑到终点
        log = env.advance()  # 推进一天
        stopped_now = set(log["stopped"])  # 今天停线的工单
        log["stopped"] = sorted(stopped_now - stopped_before)  # 只打印新开始停线的
        stopped_before = stopped_now  # 滚动到下一天
        events = {k: v for k, v in log.items() if v and k != "orders_late"}  # 除延期外有事发生的类别
        if events:  # 有事才打印
            print(f"day {env.state.day:2d} " + " ".join(f"{k}={','.join(v)}" for k, v in events.items()))  # 当天事实
    print(json.dumps(outcome_metrics(env.state), ensure_ascii=False, indent=2))  # 结果指标


if __name__ == "__main__":  # 作为模块运行时
    main()  # 执行入口
