"""文件摘要（F15 · 重试退避与熔断）

spec §5.8：限流、超时、上游错误由代码自动重试（最多 2 次，真实时间指数退避）；仍失败计入熔断；同一上游连续 3 次失败熔断 5 分钟。
- Resilience：一个对象同时提供重试参数与熔断状态，供工具代理层与模型适配层共用。
  - retryable(kind)：该错误类型是否自动重试（配置；消融 auto_retry 关闭时一律不重试）。
  - delays()：各次重试前的等待秒数；sleep(s)：可注入的睡眠函数（测试记录而不真睡）。
  - is_open(upstream)：是否熔断中（KV 里的熔断键未过期）；record_failure / record_success：连续失败计数与熔断
    （消融 circuit_breaker 关闭时不熔断）。计数与熔断键都放 KV：Redis 下多进程共享，TTL 到期自动恢复（spec §5.1 可丢失状态）。
- load_resilience_config()：读 config/resilience.yaml。
依赖：tools.kv（KV 接口）、PyYAML。被 tools.proxy、models.adapter 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import time  # 默认睡眠
from collections.abc import Callable  # 睡眠函数类型
from pathlib import Path  # 配置路径

import yaml  # 读配置

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
RESILIENCE_CONFIG = ROOT / "config" / "resilience.yaml"  # 默认配置


def load_resilience_config(path: Path = RESILIENCE_CONFIG) -> dict:
    """读重试与熔断配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


class Resilience:
    # 功能：见文件摘要。依赖：KV、配置、睡眠函数、两个消融开关。完成：熔断键 cb:open:<上游>，失败计数 cb:fail:<上游>。
    def __init__(self, kv, *, config: dict | None = None, sleep: Callable[[float], None] = time.sleep,
                 retry: bool = True, breaker: bool = True):
        self.kv = kv  # 状态存储
        self.config = config or load_resilience_config()  # 配置
        self.sleep = sleep  # 睡眠函数
        self.retry_on, self.breaker_on = retry, breaker  # 消融开关 auto_retry / circuit_breaker

    def retryable(self, kind: str | None) -> bool:
        """该错误类型是否自动重试。"""
        return self.retry_on and kind in self.config["retry"]["retryable"]  # 配置 + 开关

    def delays(self) -> list[float]:
        """各次重试前的等待秒数（指数退避）。"""
        r = self.config["retry"]  # 重试配置
        return [r["backoff_base_s"] * r["backoff_factor"] ** i for i in range(r["max_retries"])]  # 0.5、1.0……

    def is_open(self, upstream: str) -> bool:
        """是否熔断中。"""
        return self.breaker_on and self.kv.get(f"cb:open:{upstream}") is not None  # 熔断键未过期

    def record_failure(self, upstream: str) -> bool:
        """记一次（重试后仍）失败；达到阈值即熔断。返回是否因此熔断。"""
        if not self.breaker_on:  # 消融
            return False  # 不计
        b = self.config["breaker"]  # 熔断配置
        n = int(self.kv.get(f"cb:fail:{upstream}") or 0) + 1  # 连续失败次数
        if n >= b["threshold"]:  # 达到阈值
            self.kv.set(f"cb:open:{upstream}", "1", b["open_s"])  # 熔断（TTL 到期自动恢复）
            self.kv.set(f"cb:fail:{upstream}", "0", b["open_s"])  # 计数清零
            return True  # 已熔断
        self.kv.set(f"cb:fail:{upstream}", str(n), b["open_s"])  # 记计数（长时间无失败自动清）
        return False  # 未熔断

    def record_success(self, upstream: str) -> None:
        """成功一次：连续失败计数清零。"""
        if self.breaker_on:  # 开着
            self.kv.set(f"cb:fail:{upstream}", "0", self.config["breaker"]["open_s"])  # 清零
