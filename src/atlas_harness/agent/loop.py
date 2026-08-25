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
from atlas_harness.context.artifacts import ArtifactStore
from atlas_harness.context.capability import CapabilityPlan, CapabilitySelector
from atlas_harness.context.compaction import (
    REASON_MANUAL,
    CompactionResult,
    Compactor,
    compaction_reason_for,
)
from atlas_harness.context.tokens import (
    ContextBudget,
    ContextPressure,
    EstimatingCounter,
    TokenCounter,
)
from atlas_harness.events import DEFAULT_LANE, EventStore, EventType
from atlas_harness.kernel.faults import FaultInjector
from atlas_harness.model.assembler import AssembledResponse, StreamAssembler
from atlas_harness.model.protocol import (
    ModelAdapter,
    ModelMessage,
    ModelRequest,
    ModelToolCall,
    Role,
    TokenInput,
)
from atlas_harness.tools.builtin.compact_context import COMPACT_TOOL_NAME
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
FAULT_BEFORE_CONTEXT_COMPACTED = "agent_loop.before_context_compacted"
FAULT_AFTER_CONTEXT_COMPACTED = "agent_loop.after_context_compacted"
"""Crash points either side of the events the loop owns.

The loop shares the store's injector, so arming a point here and a point in the
executor exercises one continuous timeline rather than two unrelated ones.
"""

MAX_TOOL_MESSAGE_CHARS = 16_384
"""A tool result re-enters the prompt, so it is capped independently of the log."""

STEER_PREFIX = "[steer] "
FOLLOW_UP_PREFIX = "[follow-up] "


def tool_declarations(registry: ToolRegistry) -> tuple[dict[str, Any], ...]:
    """Translate registry manifests into provider-neutral tool declarations.

    Two filters apply. The registry describes tools for operators — risk, scopes,
    timeouts — and a model needs only name, purpose and arguments, so the
    operational fields are left out rather than passed along as prompt noise.

    And the shape stays dialect-free. OpenAI nests this under ``{"type":
    "function", "function": {...}}`` and renames the schema to ``parameters``;
    Anthropic takes ``input_schema`` at the top level. Neither spelling belongs
    here: each adapter owns its own wire format, so adding a provider does not
    mean first teaching it to read another provider's dialect.
    """

    return tuple(
        {
            "name": manifest.name,
            "description": manifest.description,
            "input_schema": manifest.input_schema,
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
        budget: ContextBudget | None = None,
        compactor: Compactor | None = None,
        counter: TokenCounter | None = None,
        artifacts: ArtifactStore | None = None,
        capabilities: CapabilitySelector | None = None,
        keep_recent_messages: int = 4,
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
        # The budget is derived from the model's own window, minus the room the
        # reply needs, so the thresholds mean the same thing across providers with
        # very different context sizes.
        self.budget = budget or ContextBudget.for_model(
            max_context_tokens=adapter.capabilities().max_context_tokens,
            reserve_output_tokens=max_output_tokens or 0,
        )
        self.compactor = compactor or Compactor(store, budget=self.budget)
        self.counter: TokenCounter = counter or EstimatingCounter()
        self.artifacts = artifacts or ArtifactStore(store)
        # Left as None when no memory or skill store is configured. Injecting
        # nothing is different from injecting an empty selection: the latter
        # would write a capability_injected event per iteration saying so.
        self.capabilities = capabilities
        self.keep_recent_messages = keep_recent_messages

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
            # Measured after the drain, so a steer message that pushes the prompt
            # over the mark is accounted for in the same iteration it arrives.
            self._maybe_compact(state)
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
            requested_keep = await self._run_tools(state, response)
            if requested_keep is not None:
                # The model asked for this one, so it runs regardless of pressure.
                # It happens here rather than inside the tool phase because every
                # tool message has now been appended: compacting earlier could cut
                # a call away from the result that answers it.
                self.compact(state, reason=REASON_MANUAL, keep_recent=requested_keep)

        return self._finish(state)

    def _cancelled(self, cancel: asyncio.Event | None) -> bool:
        return cancel is not None and cancel.is_set()

    # -------------------------------------------------------------- compaction

    def _prompt_tokens(self, state: RunState) -> int:
        """Size the prompt as the provider will see it, declarations included."""

        return self.counter.count(TokenInput(messages=state.messages, tools=self._declarations))

    def _maybe_compact(self, state: RunState) -> None:
        """Announce or perform a compaction, depending on how full the window is.

        The measurement is the *next prompt*, not the run's cumulative usage: the
        context window holds one request, so summing every request in the run would
        trigger a compaction on a conversation that comfortably fits.

        At the preparation mark this only records ``context_compact_pending`` —
        crossing 70% is information, not yet a reason to discard anything. From the
        automatic mark upward it compacts and replaces the working transcript.
        """

        used = self._prompt_tokens(state)
        state.prompt_tokens = used
        pressure = self.budget.pressure(used)
        if pressure is ContextPressure.OK:
            return
        if not pressure.should_compact:
            if not state.compact_pending:
                state.compact_pending = True
                self.compactor.mark_pending(
                    state.session_id,
                    operation_id=state.operation_id,
                    used_tokens=used,
                    lane_id=self.lane_id,
                    iteration=state.iterations,
                )
            return
        self.compact(
            state,
            reason=compaction_reason_for(pressure),
            used_tokens=used,
            require_replacement=True,
        )

    def compact(
        self,
        state: RunState,
        *,
        reason: str = REASON_MANUAL,
        used_tokens: int | None = None,
        keep_recent: int | None = None,
        require_replacement: bool = False,
    ) -> CompactionResult:
        """Compact now, whatever the pressure. Used by ``/compact`` and the tool.

        The rewrite is applied to the working transcript only after the event is
        recorded, so a crash between the two leaves a log that says a compaction
        happened and a prompt that is merely longer than necessary — the safe
        direction, since the transcript is rebuilt from the log on restart anyway.

        ``require_replacement`` separates the two kinds of caller. An automatic
        trigger passes it, because pressure stays high after a compaction that
        freed nothing and an event per iteration would bury the log in compactions
        that did no work. A manual trigger does not: someone asked, so "there was
        nothing to compact" is an answer worth recording.
        """

        used = self._prompt_tokens(state) if used_tokens is None else used_tokens
        self.faults.check(FAULT_BEFORE_CONTEXT_COMPACTED)
        result = self.compactor.compact(
            state.session_id,
            operation_id=state.operation_id,
            messages=state.messages,
            used_tokens=used,
            reason=reason,
            keep_recent=self.keep_recent_messages if keep_recent is None else keep_recent,
            lane_id=self.lane_id,
            iteration=state.iterations,
            require_replacement=require_replacement,
        )
        self.faults.check(FAULT_AFTER_CONTEXT_COMPACTED)
        if result.replaced_messages:
            state.replace_messages(result.messages)
            state.prompt_tokens = self._prompt_tokens(state)
        else:
            # Nothing to replace: the transcript is already system messages plus
            # the tail we were told to keep. Clearing the flag stops the pending
            # announcement from being rewritten on every later iteration.
            state.compact_pending = False
        return result

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
            messages=self._request_messages(state),
            tools=self._declarations,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
        )

    def _request_messages(self, state: RunState) -> tuple[ModelMessage, ...]:
        """The transcript plus whatever capabilities this iteration selected.

        The selection is added to the *request*, not to ``state``. A memory or
        skill is retrieved fresh for each iteration against the current user turn,
        so appending it to the transcript would accumulate stale capabilities and
        make the prompt grow with every pass; and unlike a message, an injected
        capability is not something the conversation said.

        Injection sits directly after the system prompt: it is instruction, and a
        skill placed after the transcript would read as the newest turn rather
        than as standing guidance.
        """

        plan = self._select_capabilities(state)
        if plan is None or not plan.selected:
            return state.messages
        injected = tuple(
            ModelMessage(role=item.role, content=item.content) for item in plan.items()
        )
        messages = state.messages
        head = messages[:1] if messages and messages[0].role is Role.SYSTEM else ()
        return head + injected + messages[len(head) :]

    def _select_capabilities(self, state: RunState) -> CapabilityPlan | None:
        """Select for this iteration and record what was chosen and what was not.

        The event is written even when nothing was selected, provided a candidate
        was considered: "the store held three skills and none were permitted" is
        the answer an audit needs, and an absent event cannot give it.
        """

        if self.capabilities is None:
            return None
        query = state.last_user_text()
        if not query:
            return None
        plan = self.capabilities.select(query)
        if not plan.selected and not plan.skipped:
            return plan
        self._append(
            EventType.CAPABILITY_INJECTED,
            state,
            plan.to_payload(iteration=state.iterations),
        )
        return plan

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

    async def _run_tools(self, state: RunState, response: AssembledResponse) -> int | None:
        """Execute the requested calls and append one tool message per call.

        Every call the model made gets exactly one reply, in the order it asked,
        so the transcript stays valid for providers that require it.

        Returns the ``keep_recent`` a successful ``compact_context`` call asked for,
        or ``None``. The compaction itself happens back in :meth:`run`, after every
        call has been answered: rewriting the transcript here would strand the tool
        messages this method still has to append.
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
            return None

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
                else self._result_content(state, outcome)
            )
            state.add(
                ModelMessage.tool(
                    tool_call_id=call.call_id,
                    content=content,
                    name=call.name,
                )
            )

        return self._compaction_request(outcomes)

    def _compaction_request(self, outcomes: dict[str, ToolOutcome]) -> int | None:
        """Read a ``compact_context`` call's ``keep_recent`` out of the outcomes.

        Only a *successful* call counts. A denied or failed one already told the
        model why in its tool message, and acting on it anyway would let a refused
        call still rewrite the prompt.
        """

        for outcome in outcomes.values():
            if outcome.tool_name != COMPACT_TOOL_NAME or not outcome.success:
                continue
            output = outcome.output
            if isinstance(output, dict):
                requested = output.get("keep_recent")
                if isinstance(requested, int) and requested > 0:
                    return requested
            return self.keep_recent_messages
        return None

    def _result_content(self, state: RunState, outcome: ToolOutcome) -> str:
        """Render one result for the prompt, externalizing it when it is large.

        A successful output over the inline limit is written to an artifact and the
        model receives a preview plus the artifact id instead. Failures are never
        externalized: an error message is small and is exactly what the model needs
        in full to decide its next move.

        The event log already holds the untruncated output, so the artifact is not
        the only copy — it exists so the *prompt* can carry a reference rather than
        a megabyte of build log.
        """

        if not outcome.success or not self.artifacts.should_externalize(outcome.output):
            return _tool_result_content(outcome)
        reference = self.artifacts.store(
            outcome.output,
            session_id=state.session_id,
            operation_id=state.operation_id,
            lane_id=self.lane_id,
            tool_name=outcome.tool_name,
            call_id=outcome.call_id,
        )
        return json.dumps(reference.as_context_value(), ensure_ascii=False, default=str)

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
