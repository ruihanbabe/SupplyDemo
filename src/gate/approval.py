"""文件摘要（F12 · 审批令牌与人工节点入口）

G2：写操作必须携带审批令牌，令牌由审批流程的代码签发，模型无法构造。
- ApprovalVault：令牌保管库。issue() 在主管批准时签发 HMAC 令牌，绑定草稿 ID + 版本 + 内容哈希 + 到期模拟日 + 审批事件号；
  check() 供执行器提交前按草稿查验（没有 / 过期 / 草稿改版 / 签名不符都返回原因）。令牌值只在保管库里，不进事件、日志与模型上下文。
  rebuild(session) 按 approval_granted 事件与同一密钥恢复（进程重启后用）。密钥取环境变量 SUPPLYAGENT_APPROVAL_KEY，
  没配置则为进程内随机值（重启后须重新审批）。
- HumanDesk：人工节点的代码入口（F25 Web 页、F05 模拟人工调用；模型没有这个入口）。
  approve_po / reject_po（H2）、confirm_substitute / reject_substitute（H1）写 human_decision 事件；批准采购单时先核对存在对应送审、
  草稿仍有效且内容哈希一致，再签发令牌并写 approval_granted（只记审批事件号与到期日）。
- load_gate_config()：读 config/gate.yaml。
依赖：session.log、PyYAML、标准库 hmac / hashlib / os / secrets。被 gate.evidence（查验）、F25/F05（人工操作）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import hashlib  # HMAC 摘要算法
import hmac  # HMAC 签名
import os  # 环境变量
import re  # 解析 Artifact 引用
import secrets  # 随机密钥
from pathlib import Path  # 配置路径

import yaml  # 读配置

from session.log import ART_VALID, Session  # 事件日志

ROOT = Path(__file__).resolve().parents[2]  # 仓库根
GATE_CONFIG = ROOT / "config" / "gate.yaml"  # 默认配置
KEY_ENV = "SUPPLYAGENT_APPROVAL_KEY"  # 密钥环境变量名
REF = re.compile(r"^(?P<id>.+)@v(?P<ver>\d+)$")  # Artifact 引用


def load_gate_config(path: Path = GATE_CONFIG) -> dict:
    """读门控配置。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))  # 读 YAML


# ---------------------------------------------------------------------------
# ApprovalVault：令牌保管库
# 功能：见文件摘要。
# 依赖：密钥。被 HumanDesk（签发）、EvidenceGate（查验）使用。
# 完成：按"草稿 ID@v版本"存最近一次签发的令牌与绑定信息；查验用常数时间比较。
# ---------------------------------------------------------------------------
class ApprovalVault:
    def __init__(self, key: bytes | None = None, environ=None):
        env = os.environ if environ is None else environ  # 环境变量来源
        raw = env.get(KEY_ENV)  # 配置的密钥
        self._key = key or (raw.encode("utf-8") if raw else secrets.token_bytes(32))  # 密钥
        self._tokens: dict[str, dict] = {}  # 草稿引用 → {token, hash, expires_day, approval_seq}

    def _sign(self, draft_ref: str, content_hash: str, expires_day: int, approval_seq: int) -> str:
        """令牌值：HMAC(草稿引用 | 内容哈希 | 到期日 | 审批事件号)。"""
        body = f"{draft_ref}|{content_hash}|{expires_day}|{approval_seq}"  # 绑定内容
        return hmac.new(self._key, body.encode("utf-8"), hashlib.sha256).hexdigest()  # 签名

    def issue(self, draft_ref: str, content_hash: str, expires_day: int, approval_seq: int) -> None:
        """签发（只存进保管库，不返回给调用方，避免被写进任何日志）。"""
        self._tokens[draft_ref] = {"token": self._sign(draft_ref, content_hash, expires_day, approval_seq),  # 令牌
                                   "hash": content_hash, "expires_day": expires_day,  # 绑定哈希与到期日
                                   "approval_seq": approval_seq}  # 审批事件号

    def revoke(self, draft_ref: str) -> None:
        """作废某草稿的令牌（主管驳回时）。"""
        self._tokens.pop(draft_ref, None)  # 删除

    def check(self, draft_ref: str, content_hash: str, day: int) -> str | None:
        """提交前查验：通过返回 None，否则返回原因。"""
        rec = self._tokens.get(draft_ref)  # 该草稿的令牌
        if rec is None:  # 没有
            return f"草稿 {draft_ref} 没有有效的审批令牌（须主管 H2 审批通过）"  # 原因
        if day > rec["expires_day"]:  # 过期（模拟日）
            return f"审批令牌已于第 {rec['expires_day']} 天过期，须重新送审"  # 原因
        expect = self._sign(draft_ref, content_hash, rec["expires_day"], rec["approval_seq"])  # 按当前草稿重算
        if rec["hash"] != content_hash or not hmac.compare_digest(expect, rec["token"]):  # 草稿被改或令牌不符
            return "草稿内容与审批时不一致，令牌作废，须重新送审"  # 原因
        return None  # 通过

    def values(self) -> list[str]:
        """测试辅助：全部令牌值。"""
        return [r["token"] for r in self._tokens.values()]  # 令牌

    def rebuild(self, session: Session) -> None:
        """按事件恢复：approval_granted 之后没有被驳回的草稿重新签发（须同一密钥）。"""
        self._tokens.clear()  # 清空
        for e in session.events():  # 按序重放
            p = e["payload"]  # 内容
            if e["type"] == "approval_granted":  # 批准
                self.issue(p["draft"], p["content_hash"], p["expires_day"], p["approval_seq"])  # 重签
            elif e["type"] == "human_decision" and p.get("node") == "H2" and p.get("decision") == "reject":  # 驳回
                self.revoke(p["artifact"])  # 作废


# ---------------------------------------------------------------------------
# HumanDesk：人工节点入口
# 功能：见文件摘要。
# 依赖：session、vault、门控配置。
# 完成：每个人工操作写一条 human_decision（谁、何时、对哪个 Artifact、做了什么）；批准前的核对失败抛 ValueError（页面据此报错）。
# ---------------------------------------------------------------------------
class HumanDesk:
    def __init__(self, session: Session, vault: ApprovalVault, config: dict | None = None):
        self.session, self.vault = session, vault  # 依赖
        self.config = config or load_gate_config()  # 门控配置

    def _artifact(self, ref: str) -> dict:
        """取 Artifact（含状态），引用不对抛错。"""
        m = REF.match(ref)  # 解析
        art = self.session.artifact_view(m["id"], int(m["ver"])) if m else None  # 取
        if art is None:  # 不存在
            raise ValueError(f"Artifact 不存在：{ref}")  # 报错
        return art  # 返回

    def _decide(self, node: str, ref: str, decision: str, *, by: str, day: int, note: str) -> dict:
        """写一条人工决定事件。"""
        art = self._artifact(ref)  # 目标
        return self.session.append("human_decision", day=day, actor=f"human:{by}", case_id=art["case_id"],  # 人工事件
                                   payload={"node": node, "artifact": ref, "decision": decision,  # 节点、对象、决定
                                            "by": by, "note": note})  # 谁与备注

    def approve_po(self, draft_ref: str, *, by: str, day: int, note: str = "") -> dict:
        """H2 批准采购单：核对送审与内容哈希后签发令牌。"""
        draft = self._artifact(draft_ref)  # 草稿
        asked = [e for e in self.session.events(case_id=draft["case_id"], type="approval_requested")  # 送审
                 if e["payload"]["draft"] == draft_ref]  # 这份草稿的
        if not asked:  # 没送审
            raise ValueError(f"{draft_ref} 没有送审记录")  # 报错
        if asked[-1]["payload"]["content_hash"] != draft["content_hash"] or draft["status"] != ART_VALID:  # 已变或失效
            raise ValueError(f"{draft_ref} 已失效或内容与送审时不一致，不能批准")  # 报错
        ev = self._decide("H2", draft_ref, "approve", by=by, day=day, note=note)  # 人工决定
        expires = day + self.config["approval"]["valid_days"]  # 到期模拟日
        self.vault.issue(draft_ref, draft["content_hash"], expires, ev["seq"])  # 签发（只进保管库）
        return self.session.append("approval_granted", day=day, actor="approval", case_id=draft["case_id"],  # 批准事件
                                   payload={"draft": draft_ref, "content_hash": draft["content_hash"],  # 草稿与哈希
                                            "expires_day": expires, "approval_seq": ev["seq"]},  # 到期与审批号（无令牌值）
                                   caused_by=ev["seq"])  # 因果

    def reject_po(self, draft_ref: str, *, by: str, day: int, note: str = "") -> dict:
        """H2 驳回：作废令牌。"""
        self.vault.revoke(draft_ref)  # 作废
        return self._decide("H2", draft_ref, "reject", by=by, day=day, note=note)  # 人工决定

    def confirm_substitute(self, report_ref: str, *, by: str, day: int, note: str = "") -> dict:
        """H1 工程师确认 B 级替代。"""
        return self._decide("H1", report_ref, "approve", by=by, day=day, note=note)  # 人工决定

    def reject_substitute(self, report_ref: str, *, by: str, day: int, note: str = "") -> dict:
        """H1 工程师否决替代。"""
        return self._decide("H1", report_ref, "reject", by=by, day=day, note=note)  # 人工决定
