"""文件摘要（F06 起共用 · Harness 配置读取）

读取 `config/harness.yaml`（G7）。各 Harness 组件从这里取参数与消融开关（spec §0.3），不在代码里写死。
- load_harness_config(path)：读 YAML，缺文件或缺 ablation 段即报错。
- ablation(config, name)：取某组件的消融开关；未登记的组件名报错，防止拼错键名导致组件被静默关掉。
依赖：PyYAML。被 session（失效传播开关）及后续各 Harness Feature 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 路径

import yaml  # 读 YAML

ROOT = Path(__file__).resolve().parents[2]  # 仓库根目录
HARNESS_CONFIG = ROOT / "config" / "harness.yaml"  # 默认配置文件


# 功能：读 Harness 配置。依赖：PyYAML；被各组件在构造时调用。完成：校验 ablation 段存在且全为布尔值。
def load_harness_config(path: Path = HARNESS_CONFIG) -> dict:
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML
    switches = config.get("ablation") if isinstance(config, dict) else None  # 取消融段（其他段由各组件自行读取）
    if not isinstance(switches, dict) or not all(isinstance(v, bool) for v in switches.values()):  # 格式检查
        raise ValueError("config/harness.yaml 缺少 ablation 段或其中有非布尔值")  # 报错
    return config  # 返回整份配置


# 功能：取某组件的消融开关。依赖：load_harness_config 的结果。完成：未登记的组件名直接报错。
def ablation(config: dict, name: str) -> bool:
    if name not in config["ablation"]:  # 没登记
        raise KeyError(f"消融开关未登记：{name}")  # 报错而不是默认关闭
    return config["ablation"][name]  # 返回开关值
