"""文件摘要（F07 · 可丢失辅助状态存储：缓存、日配额、令牌桶）

两个实现、同一接口（spec §5.1：Redis 只放可丢失、按真实时间过期的状态）：
- RedisKV：缓存用 SET EX；日配额与令牌桶用 Lua 脚本在 Redis 内原子执行（并行调用下不超发，spec §5.4）；令牌桶用 Redis TIME 计时。
- MemoryKV：离线测试用，真实时钟可注入，逻辑与 Lua 脚本一致。
依赖：redis（RedisKV）。被 tools.proxy（缓存、配额）与 tools.quota（QuotaGuard，也给 F08 模型端点限流）使用；F15 熔断也将放这里。
"""
from __future__ import annotations  # 类型注解延迟求值

import threading  # 内存实现的锁（模拟 Redis 的单线程原子性）
import time  # 默认真实时钟
from collections.abc import Callable  # 时钟类型
from typing import Protocol  # 接口

# 日配额：未超限则 +1 并返回新值，超限返回 -1；首次设置过期时间。
QUOTA_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return -1 end
current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2])) end
return current
"""
# 令牌桶：按流逝时间补充令牌（上限 burst），够 1 个则扣减并返回 1，否则返回 0。
BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local rate, burst = tonumber(ARGV[1]), tonumber(ARGV[2])
local s = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(s[1]) or burst
local ts = tonumber(s[2]) or now
tokens = math.min(burst, tokens + (now - ts) * rate)
local ok = 0
if tokens >= 1 then tokens = tokens - 1; ok = 1 end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(burst / rate) + 1)
return ok
"""


class KV(Protocol):
    """缓存与计数接口。"""

    def get(self, key: str) -> str | None: ...  # 取缓存
    def set(self, key: str, value: str, ttl_s: int) -> None: ...  # 写缓存（真实秒 TTL）
    def take_quota(self, key: str, limit: int, ttl_s: int) -> bool: ...  # 日配额扣 1，超限 False
    def take_token(self, key: str, rate_per_s: float, burst: float) -> bool: ...  # 令牌桶取 1，没有 False
    def set_quota(self, key: str, value: int, ttl_s: int) -> None: ...  # 重建日配额计数（F17）


# ---------------------------------------------------------------------------
# RedisKV：Redis 实现
# 功能：见文件摘要。
# 依赖：redis 客户端（调用方创建，便于测试用独立 db 或前缀）；prefix 隔离不同运行 / 测试。
# 完成：两个 Lua 脚本注册一次、按 SHA 调用。
# ---------------------------------------------------------------------------
class RedisKV:
    def __init__(self, client, prefix: str = "sa:"):
        self.r = client  # Redis 客户端
        self.prefix = prefix  # 键前缀
        self._quota = client.register_script(QUOTA_LUA)  # 日配额脚本
        self._bucket = client.register_script(BUCKET_LUA)  # 令牌桶脚本

    def get(self, key):
        value = self.r.get(self.prefix + key)  # 读
        return value.decode() if isinstance(value, bytes) else value  # 统一成字符串

    def set(self, key, value, ttl_s):
        self.r.set(self.prefix + key, value, ex=ttl_s)  # 写并设 TTL

    def take_quota(self, key, limit, ttl_s):
        return self._quota(keys=[self.prefix + key], args=[limit, ttl_s]) != -1  # 原子扣减

    def take_token(self, key, rate_per_s, burst):
        return self._bucket(keys=[self.prefix + key], args=[rate_per_s, burst]) == 1  # 原子取令牌

    def set_quota(self, key, value, ttl_s):
        self.r.set(self.prefix + key, value, ex=ttl_s)  # 直接写计数（F17 从计量重建）


# ---------------------------------------------------------------------------
# MemoryKV：内存实现
# 功能：离线测试用；clock 返回真实秒（可注入以测试 TTL 与补充速率）。
# 依赖：threading.Lock 保证并发下与 Redis 同样的原子性。
# 完成：缓存过期、日配额、令牌桶三者行为与 Lua 脚本一致。
# ---------------------------------------------------------------------------
class MemoryKV:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock  # 真实时钟
        self.data: dict[str, tuple[str, float]] = {}  # 键 → (值, 过期时刻)
        self.counters: dict[str, tuple[int, float]] = {}  # 键 → (计数, 过期时刻)
        self.buckets: dict[str, tuple[float, float]] = {}  # 键 → (令牌, 上次时刻)
        self.lock = threading.Lock()  # 原子性

    def get(self, key):
        with self.lock:  # 原子
            value = self.data.get(key)  # 取
            return value[0] if value and value[1] > self.clock() else None  # 过期视为无

    def set(self, key, value, ttl_s):
        with self.lock:  # 原子
            self.data[key] = (value, self.clock() + ttl_s)  # 写并记过期时刻

    def take_quota(self, key, limit, ttl_s):
        with self.lock:  # 原子
            count, expires = self.counters.get(key, (0, 0.0))  # 当前计数
            if expires <= self.clock():  # 已过期
                count = 0  # 重新计数
            if count >= limit:  # 超限
                return False  # 拒绝
            self.counters[key] = (count + 1, expires if count else self.clock() + ttl_s)  # +1，首次设过期
            return True  # 放行

    def set_quota(self, key, value, ttl_s):
        with self.lock:  # 原子
            self.counters[key] = (value, self.clock() + ttl_s)  # 直接写计数（F17 从计量重建）

    def take_token(self, key, rate_per_s, burst):
        with self.lock:  # 原子
            now = self.clock()  # 当前时刻
            tokens, ts = self.buckets.get(key, (burst, now))  # 当前令牌
            tokens = min(burst, tokens + (now - ts) * rate_per_s)  # 补充
            ok = tokens >= 1  # 够不够
            self.buckets[key] = (tokens - 1 if ok else tokens, now)  # 扣减
            return ok  # 结果
