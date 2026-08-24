"""The connectivity probe, offline, plus the one live check that is opt-in.

The offline tests drive the probe through ``httpx.MockTransport``, which is the
same trick the adapter's own tests use. They pin the part that has to be true
whether or not a network exists: that a fault becomes a verdict rather than a
traceback, that the exit code an operator reads follows the fault, and that the
API key never reaches the report.

The live test is the reason this file exists. Everything else in the repository
proves the adapter parses what the dialect is *documented* to send; only a real
round trip proves the endpoint an operator configured actually speaks it. It is
marked ``live`` and skipped without credentials, so the per-PR gate stays offline
and hermetic while an operator can still run it on demand.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
from typer.testing import CliRunner

from atlas_harness.config import Settings
from atlas_harness.model.probe import (
    DEFAULT_PROMPT,
    PROBE_MAX_OUTPUT_TOKENS,
    ProbeReport,
    probe,
    probe_adapter,
    probe_request,
    scrub,
)
from atlas_harness.model.protocol import StopReason
from atlas_harness.model.providers.openai_compatible import OpenAICompatibleAdapter
from atlas_harness.tools.redaction import REDACTED, redact
from atlas_harness.transport.cli import app

runner = CliRunner()

SECRET = "probe-credential-must-not-be-printed"


def _sse(*chunks: dict[str, object]) -> bytes:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


def _ready_stream() -> bytes:
    return _sse(
        {"choices": [{"index": 0, "delta": {"content": "ready"}}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        },
    )


def _adapter(
    handler: object,
    *,
    api_key: str | None = SECRET,
    base_url: str = "https://gateway.invalid/v1",
) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        model="probe-model",
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def _report(adapter: OpenAICompatibleAdapter, *, api_key: str | None = SECRET) -> ProbeReport:
    settings = Settings(model_name="probe-model")
    try:
        return await probe_adapter(
            adapter,
            probe_request(settings),
            provider="openai",
            base_url="https://gateway.invalid/v1",
            api_key=api_key,
        )
    finally:
        await adapter.aclose()


def test_the_probe_request_is_the_smallest_one_that_exercises_the_path() -> None:
    """No tools: a tool schema is the part most likely to be rejected for reasons
    that have nothing to do with whether the endpoint is reachable."""

    request = probe_request(Settings(model_name="probe-model"))

    assert request.model == "probe-model"
    assert [message.content for message in request.messages] == [DEFAULT_PROMPT]
    assert request.tools == ()
    assert request.max_output_tokens == PROBE_MAX_OUTPUT_TOKENS


async def test_a_healthy_endpoint_reports_ok_with_the_usage_it_returned() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ready_stream())

    report = await _report(_adapter(handler))

    assert report.ok is True
    assert report.completed is True
    assert report.stop_reason is StopReason.END_TURN
    assert report.text_chars == len("ready")
    assert report.usage.input_tokens == 7
    assert report.error_code is None


async def test_the_probe_sends_exactly_one_request() -> None:
    """It is billed to whoever runs it, and a retry loop would make a broken
    endpoint cost more the more broken it is."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_ready_stream())

    await _report(_adapter(handler))

    assert len(seen) == 1


async def test_a_rejected_key_is_a_verdict_not_an_exception() -> None:
    """An operator running this has a broken endpoint already; a traceback on top
    would tell them less than ``provider_http_401`` does."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": {"message": "invalid api key"}}')

    report = await _report(_adapter(handler))

    assert report.ok is False
    assert report.error_code == "provider_http_401"
    assert report.status_code == 401


async def test_a_stream_that_stops_early_is_not_ok() -> None:
    """A truncated stream would have left the agent loop with no answer, so
    reporting it as a working endpoint would be a lie the loop then discovers."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n')

    report = await _report(_adapter(handler))

    assert report.ok is False
    assert report.completed is False
    assert report.error_code == "provider_incomplete_stream"


async def test_the_report_never_carries_the_api_key() -> None:
    """The verdict is what gets pasted into a ticket. It says a key was present and
    nothing else -- not a prefix, not a length, and not the provider's echo of it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps({"error": {"message": f"key {SECRET} is revoked"}}).encode("utf-8"),
        )

    report = await _report(_adapter(handler))

    assert report.api_key_configured is True
    serialized = json.dumps(report.as_json()) + "\n".join(report.render())
    assert SECRET not in serialized


async def test_a_key_no_pattern_matches_is_still_removed_from_the_report() -> None:
    """The case shape-based redaction cannot cover, so the probe covers it by value.

    ``redact`` only knows credential shapes -- ``sk-``, ``ghp_``, ``xox``, a JWT. A
    self-hosted gateway issues keys in whatever shape it likes, and is free to quote
    one back in an error body. This key looks like an ordinary word on purpose:
    ``redact`` leaves it untouched, so if it stays out of the report it is because
    the probe removed the exact value it authenticated with.
    """

    opaque = "wednesday"
    assert redact(f"key {opaque} is revoked") == f"key {opaque} is revoked"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=json.dumps({"error": {"message": f"key {opaque} is revoked"}}).encode("utf-8"),
        )

    report = await _report(_adapter(handler, api_key=opaque), api_key=opaque)

    assert report.error is not None
    assert opaque not in report.error
    assert REDACTED in report.error


def test_scrub_is_a_no_op_without_a_key() -> None:
    """A keyless probe still redacts by shape, and must not turn ``None`` into a
    match that blanks the whole message."""

    assert scrub("nothing secret here", None) == "nothing secret here"


def test_a_missing_key_is_reported_as_configuration_not_as_a_dead_endpoint() -> None:
    """Told apart because the fixes differ: one is a wrong URL, the other is an
    unset environment variable, and the exit codes have to differ too."""

    settings = Settings(model_name="probe-model", model_api_key=None)

    report = probe(settings, provider="openai")

    assert report.ok is False
    assert report.error_code == "missing_api_key"
    assert report.api_key_configured is False


def test_the_fake_provider_probes_without_a_network() -> None:
    """So ``atlas model-check --provider fake`` is a working smoke test of the
    command itself, on a checkout with no credentials at all."""

    report = probe(Settings(model_name="fake-model"), provider="fake")

    assert report.ok is True
    assert report.text_chars > 0


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def test_model_check_exits_zero_against_the_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_MODEL_API_KEY", raising=False)

    result = runner.invoke(app, ["model-check", "--provider", "fake", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["ok"] is True
    assert payload["provider"] == "fake"


def test_model_check_exits_two_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 2 is the configuration code, which is the actual fix here. Returning
    the provider code would send an operator looking at the wrong thing."""

    monkeypatch.delenv("ATLAS_MODEL_API_KEY", raising=False)

    result = runner.invoke(app, ["model-check", "--provider", "openai"])

    assert result.exit_code == 2
    assert "verdict: failed" in result.stdout
    assert "api_key: missing" in result.stdout


def test_model_check_writes_nothing_to_the_data_directory(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connectivity check is not a session. It has to be safe to run against a
    data directory owned by something else, including a live one."""

    data_dir = tmp_path / "runtime"  # type: ignore[operator]
    monkeypatch.setenv("ATLAS_DATA_DIR", str(data_dir))

    assert runner.invoke(app, ["model-check", "--provider", "fake"]).exit_code == 0
    assert not data_dir.exists()


# --------------------------------------------------------------------------- #
# the live check
# --------------------------------------------------------------------------- #


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ATLAS_MODEL_API_KEY"),
    reason="set ATLAS_MODEL_API_KEY (and optionally ATLAS_MODEL_BASE_URL) to run the live check",
)
def test_a_real_endpoint_answers_the_probe() -> None:
    """The one test that touches a network, and the only one that can fail on a
    real provider's behaviour rather than on a recorded imitation of it.

    Deliberately not part of the per-PR gate: it needs a credential, it costs
    money, and it fails when someone else's service is down, which would make a
    red gate stop meaning "this change is broken".
    """

    report = probe(Settings())

    assert report.ok, report.render()
    assert report.api_key_configured is True
    assert report.completed is True
    assert report.usage.total_tokens > 0
