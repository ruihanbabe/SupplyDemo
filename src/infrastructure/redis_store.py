"""文件摘要（F07 起共用 · Redis 连接）

Redis 只放可丢失、按真实时间过期的辅助状态（spec §5.1）：工具缓存、配额与限流计数、熔断状态。
- redis_url()：环境变量优先，其次 .env，最后按 compose 默认端口拼本机地址（Redis 无认证、只绑 127.0.0.1）。
依赖：python-dotenv、redis。被 tools.kv.RedisKV 与集成测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import os  # 读环境变量
from pathlib import Path  # 路径

from dotenv import dotenv_values  # 读 .env

ROOT = Path(__file__).resolve().parents[2]  # 仓库根


# 功能：得到 Redis 地址。依赖：环境变量 / .env。完成：SUPPLYAGENT_REDIS_URL 优先，否则用 SUPPLYAGENT_REDIS_PORT 拼本机地址。
def redis_url() -> str:
    values = {**dotenv_values(ROOT / ".env"), **os.environ}  # 环境变量覆盖 .env
    if values.get("SUPPLYAGENT_REDIS_URL"):  # 显式地址
        return values["SUPPLYAGENT_REDIS_URL"]  # 直接用
    return f"redis://127.0.0.1:{values.get('SUPPLYAGENT_REDIS_PORT') or 6379}/0"  # compose 默认
