"""文件摘要（F11 · 凭证隔离与脱敏）

spec §5.5"凭证"：API Key 放环境变量，只由对应 MCP Server 持有；留档与追踪中的请求头、URL 参数自动脱敏。
- Credentials：只从环境变量读取的凭证容器；repr / str 不显示值；Server 构造时交给自己的后端，不经过 Runtime 或模型。
- Redactor：写出前脱敏。三条规则——① 敏感键名（不分大小写，- 与 _ 等价）的值整体替换；② 文本里 URL 查询参数同名项的值替换；
  ③ 已登记凭证的原值在任何字符串里出现都替换。返回新对象，不改原数据。
依赖：标准库 os / re。被 tools.proxy（留档、事件、错误信息写出前）与持有凭证的 Server 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import os  # 环境变量
import re  # URL 参数匹配

MASK = "***"  # 脱敏占位


def _norm(key: str) -> str:
    """键名规范化：小写，- 视同 _。"""
    return key.lower().replace("-", "_")  # 规范化


# ---------------------------------------------------------------------------
# Credentials：凭证容器
# 功能：按变量名从环境变量取值；只暴露 get() 给持有它的后端；打印时只显示变量名。
# 依赖：os.environ（可注入，测试用假值）。被分销商等外部 Server 的真实后端使用（本期只有固定样例后端与测试后端）。
# 完成：缺失的变量不报错（值为空，后端自行报"未配置"）；values() 供 Redactor 登记原值。
# ---------------------------------------------------------------------------
class Credentials:
    def __init__(self, values: dict[str, str]):
        self._values = {k: v for k, v in values.items() if v}  # 只留非空值

    @classmethod
    def from_env(cls, names: list[str], environ=None) -> Credentials:
        """从环境变量读取（environ 缺省为 os.environ）。"""
        env = os.environ if environ is None else environ  # 来源
        return cls({n: env.get(n, "") for n in names})  # 取值

    def get(self, name: str) -> str | None:
        """取一个凭证（仅后端调用）。"""
        return self._values.get(name)  # 值或 None

    def values(self) -> list[str]:
        """全部原值（仅给 Redactor 登记）。"""
        return list(self._values.values())  # 原值

    def __repr__(self) -> str:
        return f"Credentials(names={sorted(self._values)})"  # 只显示变量名

    __str__ = __repr__  # 字符串同样不显示值


# ---------------------------------------------------------------------------
# Redactor：脱敏
# 功能：见文件摘要。
# 依赖：配置 secrets.sensitive_keys。被 ToolProxy 在留档、事件、错误信息写出前调用。
# 完成：递归处理 dict / list / 字符串；登记的原值按长度从长到短替换（避免短值先替换破坏长值）。
# ---------------------------------------------------------------------------
class Redactor:
    def __init__(self, sensitive_keys: list[str]):
        self.keys = {_norm(k) for k in sensitive_keys}  # 敏感键名
        names = "|".join(sorted({re.escape(k) for k in self.keys} | {re.escape(k.replace("_", "-")) for k in self.keys}))
        self.url_param = re.compile(rf"(?i)([?&](?:{names})=)[^&#\s\"']+")  # URL 查询参数同名项
        self.values: list[str] = []  # 已登记的凭证原值

    def register(self, credentials: Credentials | None) -> None:
        """登记一组凭证原值（Server 注册到代理层时调用）。"""
        if credentials is not None:  # 有凭证
            self.values = sorted(set(self.values) | set(credentials.values()), key=len, reverse=True)  # 长的先替换

    def text(self, s: str) -> str:
        """脱敏一段文本。"""
        for v in self.values:  # 已登记原值
            s = s.replace(v, MASK)  # 替换
        return self.url_param.sub(lambda m: m.group(1) + MASK, s)  # URL 参数

    def __call__(self, obj):
        """递归脱敏，返回新对象。"""
        if isinstance(obj, dict):  # 字典：敏感键整体替换
            return {k: (MASK if isinstance(k, str) and _norm(k) in self.keys and v not in (None, "") else self(v))
                    for k, v in obj.items()}  # 逐项
        if isinstance(obj, list | tuple):  # 列表
            return [self(v) for v in obj]  # 逐项
        if isinstance(obj, str):  # 文本
            return self.text(obj)  # 脱敏
        return obj  # 其他原样
