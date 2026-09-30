"""文件摘要（F18 · 运行清单）

spec §5.12"运行清单"：每次运行记录代码 commit、模型 ID 与端点、prompt / Skill / 比对清单哈希、Policy 版本、配置快照、
种子（环境、情景、注入）、留档集 ID；Eval 结果按清单归档，清单不同的运行比较前先列出差异。
- build_manifest(seeds, models_config, archive_source)：组装清单；config/ 下全部 YAML（含比对清单）逐个记哈希，
  harness.yaml 全文作为配置快照；commit 取 git（取不到记 unknown，工作区有改动记 dirty）。
- record_manifest(session, manifest)：写 run_manifest 事件。
- diff_manifests(a, b)：把两份清单拍平后列出不同的键（"路径: 旧 → 新"）。
依赖：session.log、PyYAML、标准库 subprocess。被 Eval 运行器（F27）与测试使用。
"""
from __future__ import annotations  # 类型注解延迟求值

import subprocess  # 取 git commit
from pathlib import Path  # 文件

import yaml  # 读配置

from session.log import Session, sha256  # 事件日志与哈希

ROOT = Path(__file__).resolve().parents[2]  # 仓库根


def _git(*args: str) -> str | None:
    """跑一条 git 命令，失败返回 None。"""
    try:  # 调用
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=True)
        return out.stdout.strip()  # 输出
    except (OSError, subprocess.SubprocessError):  # 没有 git 或不是仓库
        return None  # 取不到


# 功能：组装清单。依赖：config/ 目录、模型配置、git。完成：文件哈希按相对路径排序，结果确定。
def build_manifest(*, seeds: dict, models_config: dict, archive_source: str | None = None,
                   config_dir: Path = ROOT / "config") -> dict:
    files = sorted(p for p in config_dir.rglob("*.yaml"))  # 全部配置（含比对清单、Policy）
    hashes = {str(p.relative_to(config_dir)): sha256(p.read_text(encoding="utf-8")) for p in files}  # 逐个哈希
    policy = yaml.safe_load((config_dir / "policy.yaml").read_text(encoding="utf-8"))  # Policy
    harness = yaml.safe_load((config_dir / "harness.yaml").read_text(encoding="utf-8"))  # 配置快照
    dirty = _git("status", "--porcelain", "--", "src", "config")  # 代码与配置是否有未提交改动
    return {"commit": _git("rev-parse", "HEAD") or "unknown", "dirty": bool(dirty),  # 代码版本
            "models": {n: {"provider": e["provider"], "model": e["model"]}  # 模型 ID 与端点
                       for n, e in sorted(models_config["endpoints"].items())},  # 逐个端点
            "config_hashes": hashes, "policy_version": policy.get("version"),  # 配置哈希与 Policy 版本
            "config": harness, "seeds": dict(seeds), "archive_source": archive_source}  # 配置快照、种子、留档源


def record_manifest(session: Session, manifest: dict, *, day: int = 0) -> dict:
    """写 run_manifest 事件。"""
    return session.append("run_manifest", day=day, actor="runtime", payload=manifest)  # 事件


def _flat(v, prefix: str = "") -> dict[str, object]:
    """把嵌套字典拍平成 路径 → 值。"""
    if isinstance(v, dict):  # 字典
        out = {}  # 结果
        for k, x in v.items():  # 逐项
            out.update(_flat(x, f"{prefix}.{k}" if prefix else str(k)))  # 递归
        return out  # 结果
    return {prefix: v}  # 叶子


def diff_manifests(a: dict, b: dict) -> list[str]:
    """两份清单的差异（路径: 旧 → 新），按路径排序。"""
    fa, fb = _flat(a), _flat(b)  # 拍平
    return [f"{k}: {fa.get(k)!r} → {fb.get(k)!r}" for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]  # 差异
