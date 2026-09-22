"""Layered permission evaluation: global → worker → session.

Three layers, and the rule between them is intersection, not override. Any layer that
withholds a permission withholds it, full stop. Written as override, a lower layer could
grant itself something the deployment never allowed — which is the one thing a layered
policy exists to prevent.

Every decision names the layer it came from. "Denied" alone is not actionable: the
person reading it needs to know which file to open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

import yaml

ROOT = Path(__file__).resolve().parents[2]
GLOBAL_POLICY_FILE = ROOT / "config" / "permissions.yaml"

Layer = Literal["global", "worker", "session"]

#: How much a session may do without asking. RiskEvent forces `auto` back down to `ask`
#: (D10); `explore` is the read-only posture used while investigating.
SessionMode = Literal["explore", "ask", "auto"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    #: Which layer decided. On an allow this is the last layer that had to agree; on a
    #: denial it is the outermost layer that withheld it — the most fundamental reason,
    #: and the one whose file has to change if the denial is wrong.
    layer: Layer | None
    reason: str


@dataclass(frozen=True)
class GlobalPolicy:
    """What exists in this deployment at all."""

    granted: frozenset[str] = frozenset()
    #: A kill switch that outranks every grant below it.
    revoked: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path | None = None) -> GlobalPolicy:
        source = path or GLOBAL_POLICY_FILE
        if not source.exists():
            # No file means no permissions, not all of them. A missing policy must fail
            # closed, or deleting a config file would silently widen access.
            return cls()
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        return cls(granted=frozenset(data.get("granted") or ()),
                   revoked=frozenset(data.get("revoked") or ()))


@dataclass(frozen=True)
class SessionPolicy:
    mode: SessionMode = "ask"
    #: Permissions this particular session gave up, e.g. after a RiskEvent.
    withheld: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PermissionPolicy:
    """The three layers, resolved together."""

    global_policy: GlobalPolicy = field(default_factory=GlobalPolicy)
    worker_permissions: frozenset[str] = frozenset()
    session: SessionPolicy = field(default_factory=SessionPolicy)
    worker: str | None = None

    def evaluate(self, permission: str, *, effect: str = "read") -> Decision:
        if permission in self.global_policy.revoked:
            return Decision(False, "global",
                            f"{permission} 在 config/permissions.yaml 的 revoked 名单中")
        if permission not in self.global_policy.granted:
            return Decision(False, "global",
                            f"config/permissions.yaml 未授予 {permission}")
        if permission not in self.worker_permissions:
            return Decision(False, "worker",
                            f"config/agents/{self.worker}.yaml 未声明 {permission}")
        if permission in self.session.withheld:
            return Decision(False, "session", f"本会话已撤回 {permission}")
        if effect == "write" and self.session.mode == "explore":
            return Decision(False, "session", "会话处于 explore 档，只读")
        if effect == "write" and self.session.mode == "ask":
            # Not a refusal of the permission — a refusal to act without a decision.
            # The distinction matters: the fix is an approval, not a config change.
            return Decision(False, "session", "会话处于 ask 档，写操作需人工批准")
        return Decision(True, "session", "三层均允许")


@cache
def _worker_permissions(worker: str) -> frozenset[str]:
    from infrastructure.agent_config import load_agent_config

    return frozenset(load_agent_config(worker).permissions)


def load_policy(worker: str | None, *, session: SessionPolicy | None = None,
                global_policy: GlobalPolicy | None = None) -> PermissionPolicy:
    """Assemble the chain for one caller.

    An unknown worker gets nothing rather than everything: a typo in a worker name must
    not open a door.
    """
    try:
        granted = _worker_permissions(worker) if worker else frozenset()
    except ValueError:
        granted = frozenset()
    return PermissionPolicy(
        global_policy=global_policy or GlobalPolicy.load(),
        worker_permissions=granted,
        session=session or SessionPolicy(),
        worker=worker)
