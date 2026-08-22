"""The agent loop: model -> tool -> result -> model, recorded as it goes.

One iteration is: drain the queues, ask the model, record what it said, run the
tool calls it requested, feed the results back. The loop owns the decision of
whether a tool may run at all; the model only ever produces *requests*.

Two rules shape the error handling here:

* A provider fault arrives as an event, not an exception, so the loop always has
  a place to record it and a defined stop cause.
* An invalid or refused tool call is fed back to the model as a tool result. The
  model asked for something impossible; telling it so is more useful than
  failing the run, and it keeps the transcript honest.

Cancellation is checked at the top of every iteration and again before the tool
phase, so a cancelled run stops at an iteration boundary with a terminal event
rather than mid-write.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from atlas_harness.agent.queues import CONSUMPTION_ORDER, QueueManager, QueueName
from atlas_harness.agent.state import (
    DEFAULT_SYSTEM_PROMPT,
    BudgetLimits,
    RunResult,
    RunState,
    StopCause,
    steer_messages,
)
from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.kernel.faults import FaultInjector
from atlas_harness.model.assembler import AssembledResponse, StreamAssembler
from atlas_harness.model.protocol import (
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
)
from atlas_harness.tools.executor import ToolCall, ToolExecutor, ToolOutcome
from atlas_harness.tools.redaction import redact, truncate_text
from atlas_harness.tools.registry import ToolRegistry

LOGGER = logging.getLogger(__name__)

FAULT_BEFORE_MODEL_REQUESTED = "agent_loop.before_model_requested"
FAULT_AFTER_MODEL_REQUESTED = "agent_loop.after_model_requested"
FAULT_BEFORE_ASSISTANT_MESSAGE = "agent_loop.before_assistant_message"
FAULT_AFTER_ASSISTANT_MESSAGE = "agent_loop.after_assistant_message"
FAULT_BEFORE_OPERATION_FINISHED = "agent_loop.before_operation_finished"
FAULT_AFTER_OPERATION_FINISHED = "agent_loop.after_operation_finished"
"""Crash points either side of the three events the loop owns.

The loop shares the store's injector, so arming a point here and a point in the
executor exercises one continuous timeline rather than two unrelated ones.
"""

MAX_TOOL_MESSAGE_CHARS = 16_384
"""A tool result re-enters the prompt, so it is capped independently of the log."""

STEER_PREFIX = "[steer] "
FOLLOW_UP_PREFIX = "[follow-up] "


def tool_declarations(registry: ToolRegistry) -> tuple[dict[str, Any], ...]:
    """Translate registry manifests into the function-calling shape.

    The registry describes tools for operators: risk, scopes, timeouts. A model
    needs only name, purpose and arguments, so the operational fields are left
    out rather than passed along as prompt noise.
    """

    return tuple(
        {
            "type": "function",
            "function": {
                "name": manifest.name,
                "description": manifest.description,
                "parameters": manifest.input_schema,
            },
        }
        for manifest in registry.manifests()
    )


def _tool_result_content(outcome: ToolOutcome) -> str:
    """Render one outcome as the text the model reads next.

    Failures are rendered as text too, with their stable error code, because the
    model's next move depends on knowing *why* a call failed.
    """

    if outcome.success:
        body = json.dumps(outcome.output, ensure_ascii=False, default=str)
    else:
        body = json.dumps(
            {
                "error": outcome.error_code,
                "message": outcome.error,
                "details": outcome.error_details,
            },
            ensure_ascii=False,
            default=str,
        )
    text, _ = truncate_text(body, MAX_TOOL_MESSAGE_CHARS)
    return text


def _invalid_call_content(call: ModelToolCall) -> str:
    return json.dumps(
        {
            "error": "tool_input_error",
            "message": call.error or "tool call arguments could not be parsed",
            "hint": "resend this call with a valid JSON object as arguments",
        },
        ensure_ascii=False,
    )


class AgentLoop:
    """Drive one operation to a stop cause, recording every step as an event."""

    def __init__(
        self,
        *,
        adapter: ModelAdapter,
        registry: ToolRegistry,
        executor: ToolExecutor,
        store: EventStore,
        model: str,
        provider: str = "unknown",
        limits: BudgetLimits | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        lane_id: str = DEFAULT_LANE,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.executor = executor
        self.store = store
        self.model = model
        self.provider = provider
        self.limits = limits or BudgetLimits()
        self.system_prompt = system_prompt
        self.lane_id = lane_id
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self._declarations = tool_declarations(registry)

    @property
    def faults(self) -> FaultInjector:
        """Shared with the store and the executor, so one timeline covers all three."""

        return self.store.faults

    async def run(
        self,
        prompt: str,
        *,
        session_id: str,
        operation_id: str,
        queues: QueueManager | None = None,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Run until the model answers, a budget runs out, or a fault stops it.

        The caller owns the ``operation_started`` event and the session; this
        method appends everything from the first model request to the terminal
        operation event.
        """

        state = RunState(
            session_id=session_id,
            operation_id=operation_id,
            limits=self.limits,
            system_prompt=self.system_prompt,
        )
        state.add_user(prompt)
        queue = queues or QueueManager(
            self.store,
            session_id=session_id,
            operation_id=operation_id,
            lane_id=self.lane_id,
        )

        while not state.stopped:
            if self._cancelled(cancel):
                state.stop(StopCause.CANCELLED, error="run was cancelled", code="cancelled")
                break
            if not state.iteration_budget_left():
                state.stop(
                    StopCause.MAX_ITERATIONS,
                    error=f"reached the {self.limits.max_iterations} iteration ceiling",
                    code="budget_exceeded",
                )
                break
            if not state.token_budget_left():
                state.stop(
                    StopCause.TOKEN_BUDGET,
                    error=f"reached the {self.limits.max_total_tokens} token ceiling",
                    code="budget_exceeded",
                )
                break

            state.iterations += 1
            self._drain(queue, state)
            asked = await self._ask(state, cancel=cancel)
            if asked is None:  # cancelled mid-stream; the stop cause is set
                break
            request, response = asked
            if response.failed:
                self._record_provider_error(state, request, response)
                break
            self._record_response(state, request, response)
            if not response.tool_calls:
                state.stop(StopCause.COMPLETED)
                break
            if self._cancelled(cancel):
                state.stop(StopCause.CANCELLED, error="run was cancelled", code="cancelled")
                break
            await self._run_tools(state, response)

        return self._finish(state)

    def _cancelled(self, cancel: asyncio.Event | None) -> bool:
        return cancel is not None and cancel.is_set()

    def _drain(self, queue: QueueManager, state: RunState) -> None:
        """Fold pending steer and follow-up messages into the transcript."""

        for name in CONSUMPTION_ORDER:
            drained = queue.consume(name, iteration=state.iterations)
            if not drained:
                continue
            prefix = STEER_PREFIX if name is QueueName.STEER else FOLLOW_UP_PREFIX
            state.extend(steer_messages([message.content for message in drained], prefix=prefix))

    def _build_request(self, state: RunState) -> ModelRequest:
        return ModelRequest(
            model=self.model,
            messages=state.messages,
            tools=self._declarations,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
        )

    async def _ask(
        self, state: RunState, *, cancel: asyncio.Event | None
    ) -> tuple[ModelRequest, AssembledResponse] | None:
        """Stream one model response and fold it into a finished message.

        Returns the request alongside the response so the caller can record the
        provider's ``request_id``, or ``None`` when the run was cancelled
        mid-stream, in which case the stop cause is already recorded.
        """

        request = self._build_request(state)
        self.faults.check(FAULT_BEFORE_MODEL_REQUESTED)
        self._append(
            EventType.MODEL_REQUESTED,
            state,
            {
                "provider": self.provider,
                "model": self.model,
                "request_id": request.request_id,
                "iteration": state.iterations,
                **request.summary(),
            },
        )
        self.faults.check(FAULT_AFTER_MODEL_REQUESTED)
        assembler = StreamAssembler()
        try:
            async for event in self.adapter.stream(request):
                assembler.feed(event)
                if self._cancelled(cancel):
                    break
        except asyncio.CancelledError:
            # A hard cancellation unwinds past _finish, so the terminal event has
            # to be written here or the operation would stay open on replay.
            state.stop(StopCause.CANCELLED, error="model stream was cancelled", code="cancelled")
            self._append(
                EventType.OPERATION_ABORTED,
                state,
                {"reason": "model stream was cancelled"},
            )
            raise
        if self._cancelled(cancel):
            state.stop(StopCause.CANCELLED, error="run was cancelled", code="cancelled")
            return None
        return request, assembler.finish()

    def _record_provider_error(
        self, state: RunState, request: ModelRequest, response: AssembledResponse
    ) -> None:
        """Record a failed or truncated response and stop the run."""

        error = response.error
        message = (
            "model stream ended before the message was complete"
            if error is None
            else redact(error.message)
        )
        code = "provider_incomplete_stream" if error is None else error.error_code
        self._append(
            EventType.PROVIDER_ERROR,
            state,
            {
                "provider": self.provider,
                "model": self.model,
                "request_id": request.request_id,
                "error": message,
                "error_code": code,
                "status_code": None if error is None else error.status_code,
                "retryable": False if error is None else error.retryable,
                "attempt": 1 if error is None else error.attempt,
            },
        )
        state.record_usage(response.usage)
        state.stop(StopCause.PROVIDER_ERROR, error=message, code=code)

    def _record_response(
        self, state: RunState, request: ModelRequest, response: AssembledResponse
    ) -> None:
        """Persist one successful model response and extend the transcript."""

        state.record_usage(response.usage)
        state.add(response.to_message())
        self._append(
            EventType.MODEL_STREAM_COMPLETED,
            state,
            {
                "provider": self.provider,
                "model": self.model,
                "request_id": request.request_id,
                "iteration": state.iterations,
                "stop_reason": response.stop_reason.value,
                "text_length": len(response.text),
                "tool_call_count": len(response.tool_calls),
                "invalid_tool_call_count": len(response.invalid_tool_calls),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )
        if response.text:
            self.faults.check(FAULT_BEFORE_ASSISTANT_MESSAGE)
            self._append(
                EventType.ASSISTANT_MESSAGE,
                state,
                {"content": redact(response.text), "role": "assistant"},
            )
            self.faults.check(FAULT_AFTER_ASSISTANT_MESSAGE)

    async def _run_tools(self, state: RunState, response: AssembledResponse) -> None:
        """Execute the requested calls and append one tool message per call.

        Every call the model made gets exactly one reply, in the order it asked,
        so the transcript stays valid for providers that require it.
        """

        wanted = len(response.valid_tool_calls)
        if wanted and not state.tool_budget_left(wanted):
            state.stop(
                StopCause.MAX_TOOL_CALLS,
                error=f"reached the {self.limits.max_tool_calls} tool call ceiling",
                code="budget_exceeded",
            )
            self._reply_all(
                state,
                response,
                fallback="the tool call budget for this run is exhausted",
            )
            return

        runnable = [
            ToolCall(
                tool_name=call.name,
                arguments=call.arguments,
                call_id=call.call_id,
            )
            for call in response.valid_tool_calls
        ]
        outcomes: dict[str, ToolOutcome] = {}
        if runnable:
            executed = await self.executor.execute_many(
                runnable,
                session_id=state.session_id,
                operation_id=state.operation_id,
                lane_id=self.lane_id,
            )
            state.tool_calls += len(executed)
            outcomes = {outcome.call_id: outcome for outcome in executed}

        for call in response.tool_calls:
            if not call.valid:
                state.add(
                    ModelMessage.tool(
                        tool_call_id=call.call_id,
                        content=_invalid_call_content(call),
                        name=call.name or None,
                    )
                )
                continue
            outcome = outcomes.get(call.call_id)
            content = (
                json.dumps({"error": "tool_error", "message": "tool did not produce a result"})
                if outcome is None
                else _tool_result_content(outcome)
            )
            state.add(
                ModelMessage.tool(
                    tool_call_id=call.call_id,
                    content=content,
                    name=call.name,
                )
            )

    def _reply_all(self, state: RunState, response: AssembledResponse, *, fallback: str) -> None:
        """Answer every pending call with one refusal, keeping the pairing valid."""

        body = json.dumps({"error": "budget_exceeded", "message": fallback}, ensure_ascii=False)
        for call in response.tool_calls:
            state.add(
                ModelMessage.tool(
                    tool_call_id=call.call_id,
                    content=body,
                    name=call.name or None,
                )
            )

    def _finish(self, state: RunState) -> RunResult:
        """Append the terminal operation event that matches the stop cause."""

        result = state.result()
        if result.stop_cause is StopCause.PROVIDER_ERROR:
            self._append(
                EventType.OPERATION_FAILED,
                state,
                {"error": result.error or "provider error", "error_code": result.error_code},
            )
        elif result.stop_cause is StopCause.CANCELLED:
            self._append(
                EventType.OPERATION_ABORTED,
                state,
                {"reason": result.error or "run was cancelled"},
            )
        else:
            self.faults.check(FAULT_BEFORE_OPERATION_FINISHED)
            self._append(EventType.OPERATION_FINISHED, state, {"result": result.summary()})
            self.faults.check(FAULT_AFTER_OPERATION_FINISHED)
        return result

    def _append(self, event_type: EventType, state: RunState, payload: dict[str, Any]) -> None:
        self.store.append_new(
            event_type,
            session_id=state.session_id,
            payload=payload,
            lane_id=self.lane_id,
            operation_id=state.operation_id,
        )
