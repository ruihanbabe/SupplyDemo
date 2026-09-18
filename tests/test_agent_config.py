"""Per-worker model selection: D06's cost tiers and D07's config/credential split."""
from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.agent_config import (
    AGENT_CONFIG_DIR,
    LLM_WORKERS,
    credential_keys,
    load_agent_config,
    load_all_agent_configs,
    resolve_credentials,
)
from infrastructure.llm import provider_for


def write_configs(directory: Path, **workers: str) -> Path:
    (directory / "_defaults.yaml").write_text(
        "provider: zhipu\ntemperature: 0.0\nmax_tokens: 2048\ntimeout_seconds: 60\n")
    for name, body in workers.items():
        (directory / f"{name}.yaml").write_text(body)
    return directory


# ---------- 实际仓库配置 ----------

def test_every_llm_worker_has_a_config():
    configs = load_all_agent_configs()
    assert set(configs) == set(LLM_WORKERS)
    assert all(config.model for config in configs.values())


def test_monitor_has_no_model_by_design():
    """ARCHITECTURE.md's Monitor row and D03: metric computation never calls an LLM."""
    assert "monitor" not in LLM_WORKERS
    with pytest.raises(ValueError, match="Monitor has no model by design"):
        load_agent_config("monitor")
    assert not (AGENT_CONFIG_DIR / "monitor.yaml").exists()


def test_cost_tiers_actually_differ_per_worker():
    """D06 is an architectural claim; this is the assertion that keeps it true.

    If someone flattens every worker onto one model, the decision silently stops holding
    and the only signal would be the bill.
    """
    configs = load_all_agent_configs()
    assert configs["detail"].model != configs["research"].model, (
        "D06 requires Detail (IO-bound, cheap) and Research (reasoning, flagship) "
        "to sit on different models")
    assert len({config.model for config in configs.values()}) >= 3, (
        "productinfo §13 describes distinct cost tiers; fewer than three means the "
        "matrix is not reflected in configuration")
    # Research is the one worker documented as willing to pay for quality.
    assert configs["research"].max_tokens >= max(
        config.max_tokens for worker, config in configs.items() if worker != "research")


# ---------- 合并与校验 ----------

def test_worker_file_overrides_defaults(tmp_path):
    write_configs(tmp_path, detail="worker: detail\nmodel: cheap-model\nmax_tokens: 4096\n")
    config = load_agent_config("detail", tmp_path)
    assert config.model == "cheap-model"
    assert config.max_tokens == 4096
    assert config.temperature == 0.0
    assert config.provider == "zhipu"


def test_worker_name_mismatch_is_rejected(tmp_path):
    """A copy-paste slip would route one worker's traffic to another's model."""
    write_configs(tmp_path, detail="worker: research\nmodel: x\n")
    with pytest.raises(ValueError, match="declares worker='research'"):
        load_agent_config("detail", tmp_path)


def test_missing_model_is_rejected(tmp_path):
    write_configs(tmp_path, summary="worker: summary\n")
    (tmp_path / "_defaults.yaml").write_text("provider: zhipu\n")
    with pytest.raises(ValueError, match="missing required keys: model"):
        load_agent_config("summary", tmp_path)


def test_non_mapping_config_is_rejected(tmp_path):
    write_configs(tmp_path, action="- not\n- a mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_agent_config("action", tmp_path)


# ---------- 凭据解析（D07：凭据只在 env） ----------

def test_provider_scoped_credentials_win_over_shared():
    values = {
        "SUPPLYAGENT_LLM_API_KEY": "shared-key",
        "SUPPLYAGENT_LLM_BASE_URL": "https://shared.example",
        "SUPPLYAGENT_LLM_ZHIPU_API_KEY": "zhipu-key",
        "SUPPLYAGENT_LLM_ZHIPU_BASE_URL": "https://zhipu.example/",
    }
    assert resolve_credentials("zhipu", values) == ("zhipu-key", "https://zhipu.example")
    # A second vendor with no scoped entry still resolves, so single-provider setups work.
    assert resolve_credentials("other", values) == ("shared-key", "https://shared.example")


def test_credential_key_names_are_provider_scoped():
    assert credential_keys("zhipu") == (
        "SUPPLYAGENT_LLM_ZHIPU_API_KEY", "SUPPLYAGENT_LLM_ZHIPU_BASE_URL")
    assert credential_keys("some-vendor")[0] == "SUPPLYAGENT_LLM_SOME_VENDOR_API_KEY"


def test_agent_config_never_carries_credentials(tmp_path):
    """Behaviour config is committed to Git; a key must not be reachable through it."""
    write_configs(tmp_path, detail="worker: detail\nmodel: m\napi_key: leaked-into-yaml\n")
    config = load_agent_config("detail", tmp_path)
    assert not hasattr(config, "api_key")
    assert "leaked-into-yaml" not in repr(config)


# ---------- provider 工厂 ----------

def test_provider_for_binds_the_workers_model():
    environ = {"SUPPLYAGENT_LLM_ZHIPU_API_KEY": "k", "SUPPLYAGENT_LLM_ZHIPU_BASE_URL": "https://e",
               "SUPPLYAGENT_LLM_ENABLED": "true"}
    detail = provider_for("detail", environ=environ)
    research = provider_for("research", environ=environ)
    assert detail.settings.model == load_agent_config("detail").model
    assert research.settings.model == load_agent_config("research").model
    assert detail.settings.model != research.settings.model
    assert detail.config.worker == "detail"


def test_provider_for_redaction_hides_the_key():
    environ = {"SUPPLYAGENT_LLM_ZHIPU_API_KEY": "super-secret-value",
               "SUPPLYAGENT_LLM_ZHIPU_BASE_URL": "https://e"}
    provider = provider_for("summary", environ=environ)
    assert "super-secret-value" not in str(provider.settings.redacted)
    assert provider.settings.redacted["api_key"] == "set(18 chars)"


def test_provider_for_rejects_unknown_vendor(tmp_path, monkeypatch):
    from infrastructure import llm

    monkeypatch.setitem(llm.PROVIDERS, "zhipu", llm.ZhipuProvider)
    monkeypatch.delitem(llm.PROVIDERS, "zhipu")
    with pytest.raises(Exception, match="known providers"):
        provider_for("detail", environ={"SUPPLYAGENT_LLM_API_KEY": "k"})
