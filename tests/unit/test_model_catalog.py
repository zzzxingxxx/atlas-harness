"""Capability lookup and provider factory registration."""

from __future__ import annotations

import pytest

from atlas_harness.config import Settings
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.model.catalog import (
    build_adapter,
    capabilities_for,
    known_models,
    register_provider,
    registered_providers,
    unregister_provider,
)
from atlas_harness.model.protocol import ModelAdapter
from atlas_harness.model.providers.fake import FakeAdapter
from atlas_harness.model.providers.openai_compatible import OpenAICompatibleAdapter


def test_known_model_returns_its_catalogued_limits() -> None:
    capabilities = capabilities_for("openai", "gpt-4o-mini")

    assert capabilities.provider == "openai"
    assert capabilities.max_context_tokens == 128_000
    assert capabilities.supports_tools is True


def test_unknown_model_falls_back_to_conservative_defaults() -> None:
    capabilities = capabilities_for("openai", "gpt-9-not-released-yet")

    assert capabilities.model == "gpt-9-not-released-yet"
    assert capabilities.max_context_tokens == 128_000
    assert capabilities.max_output_tokens == 4_096


def test_known_model_behind_another_gateway_keeps_its_limits() -> None:
    """A model reached through a proxy is retagged, not downgraded to defaults."""

    capabilities = capabilities_for("my-proxy", "deepseek-reasoner")

    assert capabilities.provider == "my-proxy"
    assert capabilities.max_context_tokens == 64_000
    assert capabilities.supports_thinking is True


def test_thinking_support_is_not_assumed() -> None:
    assert capabilities_for("openai", "gpt-4o").supports_thinking is False


def test_known_models_are_sorted_and_non_empty() -> None:
    entries = known_models()

    assert entries
    keys = [(entry.provider, entry.model) for entry in entries]
    assert keys == sorted(keys)


def test_builtin_providers_are_registered() -> None:
    providers = registered_providers()

    assert "fake" in providers
    assert "openai" in providers
    assert "deepseek" in providers


def test_build_adapter_uses_the_configured_provider() -> None:
    adapter = build_adapter(Settings(model_provider="fake", model_name="fake-model"))

    assert isinstance(adapter, FakeAdapter)
    assert isinstance(adapter, ModelAdapter)


def test_build_adapter_honours_an_explicit_override() -> None:
    adapter = build_adapter(
        Settings(model_provider="fake", model_name="gpt-4o-mini"), provider="openai"
    )

    assert isinstance(adapter, OpenAICompatibleAdapter)


def test_unknown_provider_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build_adapter(Settings(model_provider="nope"))

    assert excinfo.value.details["provider"] == "nope"
    assert "fake" in excinfo.value.details["known"]


def test_registering_a_provider_replaces_the_previous_factory() -> None:
    original = registered_providers()
    sentinel = FakeAdapter(model="sentinel-model")
    register_provider("test-only-provider", lambda _settings: sentinel)
    try:
        adapter = build_adapter(Settings(), provider="test-only-provider")
        assert adapter is sentinel
    finally:
        # Registration is process-global; leaving it behind would leak into the
        # `registered_providers` assertions above depending on test order.
        assert unregister_provider("test-only-provider") is True
    assert registered_providers() == original
