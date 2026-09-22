"""Per-worker model selection.

DECISIONS.md D07 splits this in two on purpose:

  behaviour  (model, sampling, tool list)  -> config/agents/*.yaml, versioned in Git
  credentials(api key, endpoint)           -> .env, never committed

D06 then requires that the two cost tiers differ per worker — Detail orchestrates tools
on a cheap fast model, Research reasons on a flagship one — so there is no single
system-wide model and no single provider. Everything here resolves by worker name.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENT_CONFIG_DIR = ROOT / "config" / "agents"
DEFAULTS_FILE = "_defaults.yaml"

#: Workers that reach a model, per DECISIONS.md D02. The three deterministic services
#: (internal, sourcing, action) are absent by design: they are pure computation and
#: orchestration, and must never call an LLM.
LLM_WORKERS = ("supervisor", "intake", "manufacturer", "adjudicator", "report")


@dataclass(frozen=True)
class AgentConfig:
    """One worker's behaviour configuration, before credentials are attached."""

    worker: str
    provider: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    tools: tuple[str, ...] = ()


def _describe(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise.

    relative_to() raises for a path outside the repository, which would replace the
    real configuration error with a confusing one about subpaths.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Agent config missing: {_describe(path)}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        # ValueError, not TypeError: every config fault in this module raises one type so
        # a caller can catch "bad agent configuration" without enumerating exceptions.
        raise ValueError(  # noqa: TRY004
            f"Agent config must be a mapping: {_describe(path)}")
    return loaded


@cache
def load_agent_config(worker: str, config_dir: Path | None = None) -> AgentConfig:
    directory = config_dir or AGENT_CONFIG_DIR
    if worker not in LLM_WORKERS:
        raise ValueError(
            f"Unknown worker {worker!r}; Monitor has no model by design. "
            f"Known workers: {', '.join(LLM_WORKERS)}")
    merged = {**_read_yaml(directory / DEFAULTS_FILE), **_read_yaml(directory / f"{worker}.yaml")}
    declared = merged.get("worker")
    if declared is not None and declared != worker:
        # A copy-paste slip here would silently route one worker's traffic to another's
        # model, and the llm_call audit row would record the wrong worker.
        raise ValueError(f"{worker}.yaml declares worker={declared!r}")
    missing = [key for key in ("provider", "model") if not merged.get(key)]
    if missing:
        raise ValueError(f"{worker}.yaml is missing required keys: {', '.join(missing)}")
    return AgentConfig(
        worker=worker,
        provider=str(merged["provider"]),
        model=str(merged["model"]),
        temperature=float(merged.get("temperature", 0.0)),
        max_tokens=int(merged.get("max_tokens", 2048)),
        timeout_seconds=float(merged.get("timeout_seconds", 60)),
        tools=tuple(merged.get("tools") or ()),
    )


def load_all_agent_configs(config_dir: Path | None = None) -> dict[str, AgentConfig]:
    return {worker: load_agent_config(worker, config_dir) for worker in LLM_WORKERS}


def credential_keys(provider: str) -> tuple[str, str]:
    """Env var names holding one provider's key and endpoint.

    Provider-scoped so two workers on different vendors can hold different keys; the
    unscoped names stay valid as a fallback for a single-provider setup.
    """
    slug = provider.strip().upper().replace("-", "_")
    return f"SUPPLYAGENT_LLM_{slug}_API_KEY", f"SUPPLYAGENT_LLM_{slug}_BASE_URL"


def resolve_credentials(
    provider: str,
    values: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (api_key, base_url), preferring provider-scoped names over the shared ones."""
    source = values if values is not None else {**_dotenv(), **os.environ}
    key_name, url_name = credential_keys(provider)
    api_key = source.get(key_name) or source.get("SUPPLYAGENT_LLM_API_KEY") or ""
    base_url = source.get(url_name) or source.get("SUPPLYAGENT_LLM_BASE_URL") or ""
    return api_key, base_url.rstrip("/")


def _dotenv() -> dict[str, str]:
    from dotenv import dotenv_values

    return {key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None}
