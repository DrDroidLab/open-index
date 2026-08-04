"""Tests for bench/config credential loading."""

from __future__ import annotations

import pytest

from bench import config


def test_ensure_llm_credentials_prefers_azure(monkeypatch) -> None:
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "https://azure.openai.azure.com/openai/v1")
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "openai-key")
    endpoint, key = config.ensure_llm_credentials()
    assert endpoint == "https://azure.openai.azure.com/openai/v1"
    assert key == "azure-key"


def test_ensure_llm_credentials_falls_back_to_openai(monkeypatch) -> None:
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "openai-key")
    endpoint, key = config.ensure_llm_credentials()
    assert endpoint == "https://api.openai.com/v1"
    assert key == "openai-key"


def test_ensure_llm_credentials_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(config, "AZURE_OPENAI_ENDPOINT", "")
    monkeypatch.setattr(config, "AZURE_OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="credentials are not configured"):
        config.ensure_llm_credentials()
