"""文件摘要（F07 · 多源矛盾警告，B2）

同一参数来自多个来源时，各来源值分开保留、不自动合并；相对差异超过容差由代码加警告（spec §5.4）。
依赖：无。被参数 Server（M2，F19/F23 时接入）与测试使用；消融开关 source_conflict_warnings 关闭时调用方不调用它。
"""
from __future__ import annotations  # 类型注解延迟求值


# 功能：比较同一参数的多个来源值。依赖：无。
# 完成：数值按"最大与最小之差 / 绝对值较大者"算相对差异；非数值只要不同就警告；缺值的来源不参与比较。
def conflict_warnings(param: str, values: dict[str, object], tolerance_pct: float) -> list[str]:
    present = {src: v for src, v in values.items() if v is not None}  # 去掉缺值的来源
    if len(present) < 2:  # 不足两个来源
        return []  # 无从比较
    listing = "，".join(f"{src}={v}" for src, v in sorted(present.items()))  # 各来源值
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present.values()):  # 全是数值
        lo, hi = min(present.values()), max(present.values())  # 最小与最大
        scale = max(abs(lo), abs(hi))  # 基准
        if scale and (hi - lo) / scale * 100 > tolerance_pct:  # 超过容差
            return [f"参数 {param} 来源不一致（{listing}），差异超过 {tolerance_pct}%，不要自行取舍，须显式标出"]  # 警告
        return []  # 在容差内
    if len({str(v) for v in present.values()}) > 1:  # 非数值且不同
        return [f"参数 {param} 来源不一致（{listing}），不要自行取舍，须显式标出"]  # 警告
    return []  # 一致
