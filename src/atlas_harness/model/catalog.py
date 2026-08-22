"""Known model capabilities and the provider factory registry.

The catalog answers two questions without any network call:

* what can this model do (context window, tools, thinking)?
* which adapter class serves this provider name?

Unknown models are not an error. They fall back to conservative defaults so a
newly released model works before this table is updated.
"""

from __future__ import annotations

from collections.abc import Callable

from atlas_harness.config import Settings
from atlas_harness.kernel.errors import ConfigurationError
from atlas_harness.model.protocol import ModelAdapter, ModelCapabilities

_FALLBACK_CONTEXT_TOKENS = 128_000
_FALLBACK_OUTPUT_TOKENS = 4_096

_CAPABILITIES: dict[str, ModelCapabilities] = {
    entry.model: entry
    for entry in (
        ModelCapabilities(
            provider="openai",
            model="gpt-4o-mini",
            max_context_tokens=128_000,
            max_output_tokens=16_384,
        ),
        ModelCapabilities(
            provider="openai",
            model="gpt-4o",
            max_context_tokens=128_000,
            max_output_tokens=16_384,
        ),
        ModelCapabilities(
            provider="openai",
            model="gpt-4.1-mini",
            max_context_tokens=1_000_000,
            max_output_tokens=32_768,
        ),
        ModelCapabilities(
            provider="deepseek",
            model="deepseek-chat",
            max_context_tokens=64_000,
            max_output_tokens=8_192,
        ),
        ModelCapabilities(
            provider="deepseek",
            model="deepseek-reasoner",
            supports_thinking=True,
            max_context_tokens=64_000,
            max_output_tokens=8_192,
        ),
        ModelCapabilities(
            provider="fake",
            model="fake-model",
            supports_thinking=True,
            max_context_tokens=8_000,
            max_output_tokens=1_024,
        ),
    )
}
"""Capabilities keyed by model name.

Model names are globally distinctive in practice, so one flat table avoids
forcing callers to know which provider hosts a given model.
"""


def capabilities_for(provider: str, model: str) -> ModelCapabilities:
    """Return capabilities for one model, falling back to safe defaults."""

    known = _CAPABILITIES.get(model)
    if known is not None and known.provider == provider:
        return known
    if known is not None:
        # Same model served through another gateway; keep the limits, retag it.
        return known.model_copy(update={"provider": provider})
    return ModelCapabilities(
        provider=provider,
        model=model,
        max_context_tokens=_FALLBACK_CONTEXT_TOKENS,
        max_output_tokens=_FALLBACK_OUTPUT_TOKENS,
    )


def known_models() -> tuple[ModelCapabilities, ...]:
    """Every catalogued model, ordered by provider then model name."""

    return tuple(sorted(_CAPABILITIES.values(), key=lambda entry: (entry.provider, entry.model)))


AdapterFactory = Callable[[Settings], ModelAdapter]
"""Builds an adapter from settings alone, so no caller handles the API key."""

_FACTORIES: dict[str, AdapterFactory] = {}


def register_provider(name: str, factory: AdapterFactory) -> None:
    """Register an adapter factory under a provider name.

    Re-registering the same name replaces the factory. That keeps tests able to
    swap in a stub without reaching into module internals.
    """

    _FACTORIES[name] = factory


def unregister_provider(name: str) -> bool:
    """Drop a registration. Returns whether the name was present.

    The counterpart to :func:`register_provider`, so a caller that installs a
    temporary adapter can undo it without touching module internals.
    """

    return _FACTORIES.pop(name, None) is not None


def registered_providers() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def build_adapter(settings: Settings, *, provider: str | None = None) -> ModelAdapter:
    """Construct the adapter for ``provider`` (default: the configured one)."""

    name = provider or settings.model_provider
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ConfigurationError(
            "unknown model provider",
            details={"provider": name, "known": list(registered_providers())},
        )
    return factory(settings)


def _register_builtin_providers() -> None:
    """Register the providers that ship with the harness.

    Imported lazily inside the function to keep ``catalog`` importable from the
    provider modules themselves without a circular import.
    """

    from atlas_harness.model.providers.fake import FakeAdapter
    from atlas_harness.model.providers.openai_compatible import OpenAICompatibleAdapter

    register_provider("fake", FakeAdapter.from_settings)
    for name in ("openai", "deepseek", "openai_compatible"):
        register_provider(name, OpenAICompatibleAdapter.from_settings)


_register_builtin_providers()
