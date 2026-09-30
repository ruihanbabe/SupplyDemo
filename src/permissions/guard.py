"""文件摘要（F11 · L1 可见性与 L2 调用时检查）

PermissionGuard 是代理层管道第①步（spec §5.4、§5.5）：
- visible(token, tools)：L1 可见性 = 令牌工具 ∩ 执行者角色工具 ∩ 当前模式允许的风险级；R1/R2 另要求执行者在 write_executors。
- check(token, name, tool, params)：L2 每次调用重新检查——验签 → 工具存在 → 可见 → 作用域（scoped 工具的料号 ⊆ 令牌作用域）；
  拒绝返回 Denial（错误类型、原因、合法路径引导）。编造的工具按"不存在"并附可见工具清单（A6）。
依赖：permissions.tokens（Authority、CapabilityToken）；工具定义只按属性读取（name / risk / scoped / entity_keys），不反向依赖 tools。
被 tools.proxy（每次调用）、runtime.loop（给模型的工具表）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import re  # 引导匹配
from dataclasses import dataclass  # 拒绝结果

from permissions.tokens import Authority, CapabilityToken  # 令牌与签发者


# 功能：一次拒绝。依赖：无。完成：错误类型（forbidden / not_found）、给模型的原因（含引导）、拒绝类别（事件统计用）。
@dataclass
class Denial:
    kind: str  # forbidden / not_found
    message: str  # 原因 + 合法路径引导
    category: str  # token / unknown / role / write / mode / scope / case


class PermissionGuard:
    # 功能：见文件摘要。依赖：Authority（验签、角色工具、作用域开关）与其配置。完成：配置里的引导按工具名正则匹配。
    def __init__(self, authority: Authority):
        self.authority = authority  # 签发者（验签）
        self.config = authority.config  # 权限配置
        self.guidance = [(re.compile(g["pattern"]), g["hint"]) for g in self.config.get("guidance", [])]  # 引导规则

    def _allowed(self, token: CapabilityToken, name: str, tool) -> str | None:
        """L1 判定：可见返回 None，否则返回不可见的类别（role / write / mode）。"""
        if name not in token.tools or name not in self.authority.role_tools(token.executor):  # 不在令牌或角色内
            return "role"  # 角色不符
        risk = getattr(tool, "risk", "R0")  # 工具风险级
        if risk != "R0" and token.executor not in self.config["write_executors"]:  # 写工具仅编排者
            return "write"  # 写权限
        if risk not in self.config["modes"][token.mode]:  # 模式不允许该级别
            return "mode"  # 模式
        return None  # 可见

    def visible(self, token: CapabilityToken, tools: dict) -> list[str]:
        """L1：该令牌持有者能看到的工具名（按名排序）；令牌不可信时为空。"""
        if not self.authority.verify(token):  # 验签失败
            return []  # 什么都看不到
        return sorted(n for n, t in tools.items() if self._allowed(token, n, t) is None)  # 过滤

    def hint(self, name: str, category: str) -> str:
        """合法路径引导：先按工具名匹配配置规则，再按拒绝类别。"""
        for pattern, text in self.guidance:  # 工具名规则（如改排产 → 上报计划员）
            if pattern.search(name):  # 命中
                return text  # 用它
        return self.config["hints"].get(category, "")  # 按类别

    def check(self, token, name: str, tool, tools: dict) -> Denial | None:
        """L2：通过返回 None，否则返回 Denial（作用域要用校验后的参数，在参数校验后另调 check_scope）。"""
        if not self.authority.verify(token):  # 身份无法核验
            return Denial("forbidden", f"调用被拒：{self.config['hints']['token']}", "token")  # 拒绝
        visible = self.visible(token, tools)  # 可见工具
        if tool is None:  # 编造的工具（A6）
            hint = self.hint(name, "unknown")  # 引导
            return Denial("not_found", f"工具 {name!r} 不存在，可用工具：{', '.join(visible) or '无'}"  # 不存在
                          + (f"。{hint}" if hint else ""), "unknown")  # 附引导
        category = self._allowed(token, name, tool)  # L1 判定
        if category is not None:  # 不可见
            return Denial("forbidden", f"工具 {name!r} 不在你的权限内（{token.actor}）。{self.hint(name, category)}"
                          f"。可用工具：{', '.join(visible) or '无'}", category)  # 原因 + 引导 + 清单
        return None  # 通过

    def check_scope(self, token: CapabilityToken, name: str, tool, params) -> Denial | None:
        """L2 作用域：scoped 工具涉及的料号必须全部在令牌作用域内（消融或作用域不限时不检查）。"""
        if not self.authority.scope_enabled or token.scope is None or not getattr(tool, "scoped", False):  # 不检查
            return None  # 通过
        mpns = set(tool.entity_keys(params).get("mpn", []))  # 本次涉及的料号
        outside = sorted(mpns - token.scope) if mpns else ["（未指定料号）"]  # 越界料号（不指定料号 = 可能查全部）
        if not outside:  # 都在作用域内
            return None  # 通过
        return Denial("forbidden", f"料号超出本任务作用域：{', '.join(outside)}。{self.hint(name, 'scope')}", "scope")
