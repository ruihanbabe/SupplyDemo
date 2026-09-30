"""文件摘要（F11 · 能力令牌）

能力令牌由代码签发（spec §5.5）：写明持有者（执行者 + 案子）、可用工具、作用域（料号集合，None = 不限）、案子模式、
父令牌与代表链，HMAC 签名。令牌对象只由 Runtime 持有并交给代理层，模型只见到其结果；事件里只记令牌号与代表链。
- CapabilityToken：不可变令牌；actor 为 Runtime 标注的身份"执行者@案子"；chain() 给出写进事件 meta 的代表链。
- Authority：签发根令牌（编排者，每个活跃段一次）、签发子令牌（权限衰减：工具与作用域只能收窄，越界抛 AttenuationError）、验签。
  令牌号按签发顺序确定性编号（保证同种子重跑的事件哈希链一致）；签名密钥为进程内随机值，签名不进任何日志。
- load_permissions_config()：读 config/permissions.yaml。
依赖：PyYAML、标准库 hmac / hashlib / secrets。被 permissions.guard、tools.proxy、runtime.segment / dispatch 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import hashlib  # 签名摘要算法
import hmac  # HMAC 签名
import json  # 规范化签名内容
import secrets  # 进程内随机密钥
from dataclasses import dataclass, replace  # 不可变数据类与替换字段
from pathlib import Path  # 配置路径

import yaml  # 读配置

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
PERMISSIONS_CONFIG = ROOT / "config" / "permissions.yaml"  # 默认配置


def load_permissions_config(path: Path = PERMISSIONS_CONFIG) -> dict:
    """读权限策略配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


class AttenuationError(ValueError):
    """子令牌越出父令牌（工具或作用域），拒绝签发。"""


# ---------------------------------------------------------------------------
# CapabilityToken：能力令牌
# 功能：一次任务（或一个活跃段）的全部权限声明；frozen 保证签发后不可改，改字段即验签失败。
# 依赖：无；被 Authority 签发、被 PermissionGuard 检查。
# 完成：身份字段（执行者、案子）、能力字段（工具、作用域、模式）、代表链字段（父令牌、替谁、由谁发起）、签名。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CapabilityToken:
    token_id: str  # 令牌号（确定性编号）
    executor: str  # 持有者执行者
    case_id: str | None  # 所属案子
    tools: frozenset[str]  # 可用工具（能力上限，实际可见还要 ∩ 角色 ∩ 模式）
    scope: frozenset[str] | None  # 料号作用域（None = 不限）
    mode: str  # 案子模式（S1 / S3）
    parent: str | None  # 父令牌号（根令牌为 None）
    on_behalf_of: str | None  # 替谁行动（父令牌持有者身份）
    initiated_by: str  # 由哪个人或事件发起（开案事件的执行者与序号）
    signature: str = ""  # HMAC 签名（签发时填）

    @property
    def actor(self) -> str:
        """Runtime 标注的身份：执行者@案子（不信模型自报）。"""
        return f"{self.executor}@{self.case_id}"  # 身份串

    def body(self) -> str:
        """参与签名的规范化内容（除签名外的全部字段）。"""
        return json.dumps({"id": self.token_id, "executor": self.executor, "case": self.case_id,  # 身份
                           "tools": sorted(self.tools),  # 工具
                           "scope": sorted(self.scope) if self.scope is not None else None,  # 作用域
                           "mode": self.mode, "parent": self.parent, "for": self.on_behalf_of,  # 模式与链
                           "by": self.initiated_by}, ensure_ascii=False, sort_keys=True)  # 发起者

    def chain(self, risk: str | None = None) -> dict:
        """代表链（写进事件 meta）：子 Agent → 替哪个编排者 → 哪个案子 → 由谁发起；可附本次动作风险级。"""
        out = {"token": self.token_id, "executor": self.executor, "on_behalf_of": self.on_behalf_of,  # 谁替谁
               "case_id": self.case_id, "initiated_by": self.initiated_by, "mode": self.mode}  # 案子、发起者、模式
        if risk is not None:  # 有动作风险级
            out["risk"] = risk  # 附上
        return out  # 代表链


# ---------------------------------------------------------------------------
# Authority：签发与验签
# 功能：根令牌与子令牌签发、权限衰减、验签；按消融开关 capability_scope 决定是否处理作用域。
# 依赖：permissions 配置（executors、default_mode）。被 SegmentRunner（根）、Dispatcher（子）、ToolProxy（验签）使用。
# 完成：根令牌工具 = 配置里全部执行者工具之并；子令牌工具 = 父 ∩ 子角色 ∩ 请求；作用域须为父子集。
# ---------------------------------------------------------------------------
class Authority:
    def __init__(self, config: dict, *, scope_enabled: bool = True, key: bytes | None = None):
        self.config = config  # 权限配置
        self.scope_enabled = scope_enabled  # 消融开关 capability_scope
        self._key = key or secrets.token_bytes(32)  # 签名密钥（进程内，不落盘、不进日志）
        self._n = 0  # 已签发数（令牌号）

    def _sign(self, token: CapabilityToken) -> CapabilityToken:
        """编号后签名。"""
        self._n += 1  # 计数
        token = replace(token, token_id=f"cap-{self._n}")  # 确定性令牌号
        sig = hmac.new(self._key, token.body().encode("utf-8"), hashlib.sha256).hexdigest()  # 签名
        return replace(token, signature=sig)  # 带签名的令牌

    def verify(self, token) -> bool:
        """验签：必须是本 Authority 签发、且签发后未被改动。"""
        if not isinstance(token, CapabilityToken) or not token.signature:  # 类型不对或未签名
            return False  # 拒绝
        sig = hmac.new(self._key, token.body().encode("utf-8"), hashlib.sha256).hexdigest()  # 重算
        return hmac.compare_digest(sig, token.signature)  # 常数时间比较

    def role_tools(self, executor: str) -> frozenset[str]:
        """执行者角色的工具（配置里没有的执行者 = 空）。"""
        return frozenset(self.config["executors"].get(executor) or [])  # 角色工具

    def issue_root(self, *, executor: str, case_id: str | None, mode: str | None, initiated_by: str,
                   scope: set[str] | None = None) -> CapabilityToken:
        """签发根令牌：工具为本案能力上限（全部执行者工具之并）；模式缺省按配置。"""
        mode = mode or self.config["default_mode"]  # 模式
        if mode not in self.config["modes"]:  # 未知模式
            raise ValueError(f"未知案子模式：{mode}")  # 报错
        tools = frozenset().union(*(self.role_tools(e) for e in self.config["executors"]))  # 能力上限
        scope = frozenset(scope) if (scope is not None and self.scope_enabled) else None  # 作用域
        return self._sign(CapabilityToken("", executor, case_id, tools, scope, mode, None, None, initiated_by))

    def attenuate(self, parent: CapabilityToken, *, executor: str, tools: list[str] | None = None,
                  scope: list[str] | None = None) -> CapabilityToken:
        """签发子令牌：只能收窄。请求的工具超出父令牌或子角色、作用域超出父作用域时抛 AttenuationError。"""
        if not self.verify(parent):  # 父令牌不可信
            raise AttenuationError("父令牌验签失败")  # 拒绝
        allowed = parent.tools & self.role_tools(executor)  # 父 ∩ 子角色
        if tools is not None:  # 显式请求了工具
            extra = set(tools) - allowed  # 越界部分
            if extra:  # 有越界
                raise AttenuationError(f"子任务请求的工具超出授权：{', '.join(sorted(extra))}")  # 拒绝
            allowed = frozenset(tools)  # 收窄到请求
        child_scope = parent.scope  # 默认继承父作用域
        if self.scope_enabled and scope is not None:  # 显式作用域
            if parent.scope is not None and not set(scope) <= parent.scope:  # 超出父作用域
                raise AttenuationError(f"作用域超出父任务：{', '.join(sorted(set(scope) - parent.scope))}")
            child_scope = frozenset(scope)  # 收窄
        if not self.scope_enabled:  # 消融：不处理作用域
            child_scope = None  # 不限
        return self._sign(CapabilityToken("", executor, parent.case_id, allowed, child_scope, parent.mode,  # 继承案子与模式
                                          parent.token_id, parent.actor, parent.initiated_by))  # 代表链
