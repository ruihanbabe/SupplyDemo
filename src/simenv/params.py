"""文件摘要（F03 环境内核 · 参数）

本文件把 `config/env/*.yaml` 读成一个经过校验的参数字典（G7：业务参数来自配置）。
- load_params(path)：读 YAML 并校验必需键、区间合法、天数 ≥ 1，不合法直接报错，不带默认值兜底。
依赖：PyYAML。被 seed（生成初始状态）与 cli（sim-run）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 配置文件路径

import yaml  # 读 YAML 配置

ROOT = Path(__file__).resolve().parents[2]  # 仓库根目录（src/simenv 往上两级）
DEFAULT_PARAMS = ROOT / "config" / "env" / "default.yaml"  # 默认参数文件


class ParamsError(ValueError):
    """环境参数配置不合法。"""


# ---------------------------------------------------------------------------
# load_params：读取并校验环境参数
# 功能：返回原样结构的参数字典；任何缺键、区间颠倒、天数 < 1 都报错，避免带病生成环境。
# 依赖：_range 辅助校验；被 seed.generate 与 cli 调用。
# 完成：校验总天数、产线、分销商天数、每块板订单规则、工单与库存区间、在途比例。
# ---------------------------------------------------------------------------
def load_params(path: Path = DEFAULT_PARAMS) -> dict:
    p = yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML
    try:  # 缺键统一转成 ParamsError
        if p["horizon_days"] < 1:  # 模拟总天数须为正
            raise ParamsError("horizon_days 须 ≥ 1")  # 报错
        if not p["lines"]:  # 至少一条产线
            raise ParamsError("lines 不能为空")  # 报错
        for name, d in p["distributors"].items():  # 每个分销商
            if d["ship_days"] < 1 or d["transit_days"] < 1:  # 写入发生在当日机制之后，发货最早次日
                raise ParamsError(f"分销商 {name} 的 ship_days / transit_days 须 ≥ 1")  # 报错
        for board, rule in p["boards"].items():  # 每块板的订单规则
            _range(rule["orders"], f"boards.{board}.orders", 1)  # 订单张数区间
            _range(rule["qty"], f"boards.{board}.qty", 1)  # 台数区间
            if rule["qty_step"] < 1:  # 台数步长
                raise ParamsError(f"boards.{board}.qty_step 须 ≥ 1")  # 报错
        _range(p["orders"]["due_day"], "orders.due_day", 1)  # 到期日区间
        _range(p["work_orders"]["duration_days"], "work_orders.duration_days", 1)  # 工期区间
        if p["work_orders"]["batch_max"] < 1 or p["work_orders"]["lead_days"] < 0:  # 批量与缓冲
            raise ParamsError("work_orders.batch_max 须 ≥ 1，lead_days 须 ≥ 0")  # 报错
        _range(p["initial_stock"]["cover_days"], "initial_stock.cover_days", 0)  # 在库覆盖区间
        _range(p["in_transit"]["eta_day"], "in_transit.eta_day", 1)  # 在途到货区间
        for key in ("initial_stock", "in_transit"):  # 两个比例参数
            frac = p[key]["short_fraction" if key == "initial_stock" else "fraction"]  # 取比例
            if not 0 <= frac <= 1:  # 比例在 [0,1]
                raise ParamsError(f"{key} 的比例须在 [0, 1]：{frac!r}")  # 报错
        if p["in_transit"]["cover_extra_days"] < 0:  # 在途覆盖天数非负
            raise ParamsError("in_transit.cover_extra_days 须 ≥ 0")  # 报错
    except (KeyError, TypeError) as exc:  # 缺键或类型不对
        raise ParamsError(f"环境参数缺少或类型错误：{exc}") from None  # 统一报错
    return p  # 校验通过，原样返回


# ---------------------------------------------------------------------------
# _range：校验一个 [下限, 上限] 闭区间
# 功能：两个整数、下限 ≤ 上限、下限不小于给定最小值。
# 依赖：被 load_params 调用。
# 完成：错误信息带配置路径，便于定位。
# ---------------------------------------------------------------------------
def _range(value: list, name: str, minimum: int) -> None:
    if (len(value) != 2 or not all(isinstance(v, int) for v in value)  # 必须是两个整数
            or value[0] > value[1] or value[0] < minimum):  # 且有序、不低于最小值
        raise ParamsError(f"{name} 须为 [下限, 上限] 且下限 ≥ {minimum}：{value!r}")  # 报错
