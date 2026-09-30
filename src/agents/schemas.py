"""文件摘要（F23 · 执行者的输出模型）

每个执行者只产出一种 Artifact（spec §3.1 原则 3），输出模型即结构约束：多写字段（如替代检索写"等级"）一律校验不过、触发 F08 修复。
- Citation：引用（料号、datasheet 版本、页码，可带数值或原文片段），由 datasheets.cite 逐条校验。
- PartProfileOut / CandidateSetOut / VerificationOut：三个子 Agent 的输出；ResolveChoice / ExplanationOut：两个 LLM 函数的输出。
依赖：pydantic。被 agents.nodes 使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from typing import Literal  # 取值

from pydantic import BaseModel, ConfigDict, Field  # 输出模型


class Strict(BaseModel):
    """不允许多写字段（执行者不替上下游干活）。"""
    model_config = ConfigDict(extra="forbid")  # 多写即校验不过


class Citation(Strict):
    """一处引用：datasheet 版本 + 页码，可带数值或原文片段。"""
    mpn: str  # 文档键（料号或 board:<板>）
    version: str  # datasheet 版本（内容哈希）
    page: int = Field(ge=1)  # 页码
    value: str | float | int | None = None  # 引用的数值
    quote: str | None = None  # 引用的原文片段


class Constraint(Strict):
    """原料在这块板上的一条约束。"""
    param: str  # 参数名
    requirement: str  # 要求（如 "≥ 25 dB @ 2.4 GHz"）
    hard: bool  # 硬约束（不满足就不能替换）
    citation: Citation | None = None  # 证据


class PartProfileOut(Strict):
    """原料画像输出。"""
    mpn: str  # 料号
    board: str | None = None  # 板
    constraints: list[Constraint] = Field(min_length=1)  # 约束


class Candidate(Strict):
    """一个候选（只有料号与说明，不评等级）。"""
    mpn: str  # 候选料号
    note: str = ""  # 来源说明


class CandidateSetOut(Strict):
    """替代检索输出。"""
    original: str  # 原料号
    source: Literal["kb"]  # 来源（本期只有知识库 B；DigiKey 待授权）
    candidates: list[Candidate]  # 候选


class ItemResult(Strict):
    """比对清单一项的结论。"""
    id: str  # 比对项 ID（来自清单）
    result: Literal["satisfied", "not_satisfied", "insufficient"]  # 满足 / 不满足 / 证据不足
    citations: list[Citation] = Field(default_factory=list)  # 证据


class VerificationOut(Strict):
    """Verifier 输出（不含等级：等级由代码汇总）。"""
    candidate: str  # 候选
    original: str  # 原料
    items: list[ItemResult]  # 逐项结论


class ResolveChoice(Strict):
    """Resolver 消歧：只能从候选里选。"""
    mpn: str | None  # 选中的料号（都不像就为空）
    confidence: float = Field(ge=0, le=1)  # 置信度
    reason: str  # 一句理由


class ExplanationOut(Strict):
    """Explainer 输出。"""
    text: str = Field(min_length=1)  # 说明文字
