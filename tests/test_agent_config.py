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
from infrastructure.llm import backend_for


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


def test_per_worker_model_override_actually_takes_effect(tmp_path):
    """D06's mechanism: one worker's model can change without touching the others.

    This used to assert that Intake and Manufacturer sat on *different* models. All five
    workers are currently pinned to one model as a deliberate configuration choice, so
    that assertion no longer holds and would only be satisfied by undoing the choice.
    What still must hold — and what D06 actually depends on — is that the tiers remain
    separable: changing one file changes exactly one worker. If this breaks, restoring
    cost tiers later would be impossible.

    The guard this replaces is gone: nothing now fails if every worker is expensive.
    Cost control is a configuration decision here, not something tests can enforce.
    """
    write_configs(
        tmp_path,
        evidence_check="worker: evidence_check\nmodel: cheap-tier\n",
        spec_check="worker: spec_check\nmodel: flagship-tier\nmax_tokens: 8192\n",
        proposal="worker: proposal\nmodel: mid-tier\n",
    )
    assert load_agent_config("evidence_check", tmp_path).model == "cheap-tier"
    assert load_agent_config("spec_check", tmp_path).model == "flagship-tier"
    assert load_agent_config("proposal", tmp_path).model == "mid-tier"
    # Sampling limits stay per-worker even when the model is shared.
    assert load_agent_config("spec_check", tmp_path).max_tokens == 8192
    assert load_agent_config("evidence_check", tmp_path).max_tokens == 2048


def test_repository_config_is_internally_consistent():
    """Whatever the models are, every worker must resolve to a usable configuration."""
    configs = load_all_agent_configs()
    assert all(config.provider for config in configs.values())
    assert all(config.max_tokens > 0 for config in configs.values())
    assert all(config.worker == name for name, config in configs.items())


# ---------- 合并与校验 ----------

def test_worker_file_overrides_defaults(tmp_path):
    write_configs(tmp_path, evidence_check="worker: evidence_check\nmodel: cheap-model\nmax_tokens: 4096\n")
    config = load_agent_config("evidence_check", tmp_path)
    assert config.model == "cheap-model"
    assert config.max_tokens == 4096
    assert config.temperature == 0.0
    assert config.provider == "zhipu"


def test_worker_name_mismatch_is_rejected(tmp_path):
    """A copy-paste slip would route one worker's traffic to another's model."""
    write_configs(tmp_path, evidence_check="worker: spec_check\nmodel: x\n")
    with pytest.raises(ValueError, match="declares worker='spec_check'"):
        load_agent_config("evidence_check", tmp_path)


def test_missing_model_is_rejected(tmp_path):
    write_configs(tmp_path, proposal="worker: proposal\n")
    (tmp_path / "_defaults.yaml").write_text("provider: zhipu\n")
    with pytest.raises(ValueError, match="missing required keys: model"):
        load_agent_config("proposal", tmp_path)


def test_non_mapping_config_is_rejected(tmp_path):
    write_configs(tmp_path, evidence_check="- not\n- a mapping\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_agent_config("evidence_check", tmp_path)


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
    write_configs(tmp_path, evidence_check="worker: evidence_check\nmodel: m\napi_key: leaked-into-yaml\n")
    config = load_agent_config("evidence_check", tmp_path)
    assert not hasattr(config, "api_key")
    assert "leaked-into-yaml" not in repr(config)


# ---------- backend 工厂 ----------

def test_backend_for_binds_the_workers_model():
    environ = {"SUPPLYAGENT_LLM_ZHIPU_API_KEY": "k", "SUPPLYAGENT_LLM_ZHIPU_BASE_URL": "https://e",
               "SUPPLYAGENT_LLM_ENABLED": "true"}
    evidence_check = backend_for("evidence_check", environ=environ)
    spec_check = backend_for("spec_check", environ=environ)
    # Each provider carries the model its own YAML declares. Whether those values
    # differ is a configuration choice, not something this test should pin down.
    assert evidence_check.settings.model == load_agent_config("evidence_check").model
    assert spec_check.settings.model == load_agent_config("spec_check").model
    assert evidence_check.config.worker == "evidence_check"
    assert spec_check.config.worker == "spec_check"
    assert evidence_check.config.max_tokens != spec_check.config.max_tokens


def test_backend_for_redaction_hides_the_key():
    environ = {"SUPPLYAGENT_LLM_ZHIPU_API_KEY": "super-secret-value",
               "SUPPLYAGENT_LLM_ZHIPU_BASE_URL": "https://e"}
    provider = backend_for("proposal", environ=environ)
    assert "super-secret-value" not in str(provider.settings.redacted)
    assert provider.settings.redacted["api_key"] == "set(18 chars)"


def test_backend_for_rejects_unknown_vendor(tmp_path, monkeypatch):
    from infrastructure import llm

    monkeypatch.setitem(llm.BACKENDS, "zhipu", llm.OpenAICompatibleBackend)
    monkeypatch.delitem(llm.BACKENDS, "zhipu")
    with pytest.raises(Exception, match="known backends"):
        backend_for("evidence_check", environ={"SUPPLYAGENT_LLM_API_KEY": "k"})
