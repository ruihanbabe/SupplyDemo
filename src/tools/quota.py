"""文件摘要（F07 · 配额与限流守卫）

QuotaGuard 按 config/tools.yaml 的 providers 段，对每个提供方做"日配额 + 令牌桶"（spec §5.4），状态放 KV（Redis）。
- try_acquire(provider)：放行返回 None，不放行返回原因（daily_quota / rate_limited）。
- acquire(provider)：F08 Limiter 接口；不放行抛 ModelUnavailable('rate_limited')，模型端点复用同一组件。
日配额按真实 UTC 日期分桶（外部服务的配额按真实日计），键 TTL 2 天自动清理；丢失时只会多放行，不影响正确性（spec §5.1）。
依赖：tools.kv、models.types。被 tools.proxy 与 models.adapter 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from collections.abc import Callable  # 日期函数类型
from datetime import UTC, datetime  # 真实日期

from models.types import ModelUnavailable  # 模型端点限流异常
from tools.kv import KV  # 存储接口


def _today() -> str:
    """真实 UTC 日期。"""
    return datetime.now(UTC).strftime("%Y-%m-%d")  # 如 2026-09-30


class QuotaGuard:
    # 功能：见文件摘要。依赖：KV、providers 配置。完成：未配置的提供方直接拒绝（防止漏配后无限调用）。
    def __init__(self, kv: KV, providers: dict, today: Callable[[], str] = _today):
        self.kv = kv  # 存储
        self.providers = providers  # 提供方配置
        self.today = today  # 真实日期函数

    def try_acquire(self, provider: str) -> str | None:
        conf = self.providers.get(provider)  # 该提供方配置
        if conf is None:  # 未配置
            return "unconfigured"  # 拒绝
        if conf["daily_limit"] is not None and not self.kv.take_quota(  # 日配额
                f"quota:{provider}:{self.today()}", conf["daily_limit"], 2 * 86400):  # 按日分桶
            return "daily_quota"  # 日配额用完
        if conf["rate_per_s"] is not None and not self.kv.take_token(  # 令牌桶
                f"bucket:{provider}", conf["rate_per_s"], conf["burst"]):  # 速率与突发
            return "rate_limited"  # 限流
        return None  # 放行

    def acquire(self, provider: str) -> None:
        reason = self.try_acquire(provider)  # 检查
        if reason is not None:  # 不放行
            raise ModelUnavailable("rate_limited", f"{provider}: {reason}")  # F08 按限流处理
