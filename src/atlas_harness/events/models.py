"""Versioned event envelope and typed payloads."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny, model_validator

from atlas_harness.kernel.errors import EventValidationError
from atlas_harness.kernel.ids import IdFactory

DEFAULT_LANE = "main"

CURRENT_SCHEMA_VERSION = 8
"""Version written by this build. M10 added the recorded intent classification."""

SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7, 8})
"""Versions this build can read. v1-v7 logs from M1-M9 stay replayable."""


class EventType(StrEnum):
    SESSION_CREATED = "session_created"
    OPERATION_STARTED = "operation_started"
    MODEL_REQUESTED = "model_requested"
    ASSISTANT_MESSAGE = "assistant_message"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    TOOL_STARTED = "tool_started"
    TOOL_RESULT = "tool_result"
    OPERATION_FINISHED = "operation_finished"
    OPERATION_FAILED = "operation_failed"
    OPERATION_ABORTED = "operation_aborted"
    OPERATION_SUSPENDED = "operation_suspended"
    OPERATION_RESUMED = "operation_resumed"
    SNAPSHOT_CREATED = "snapshot_created"
    LANE_CREATED = "lane_created"
    BRANCH_CREATED = "branch_created"
    BRANCH_SWITCHED = "branch_switched"
    MODEL_STREAM_COMPLETED = "model_stream_completed"
    PROVIDER_ERROR = "provider_error"
    QUEUE_MESSAGE_ENQUEUED = "queue_message_enqueued"
    QUEUE_MESSAGE_CONSUMED = "queue_message_consumed"
    CONTEXT_COMPACT_PENDING = "context_compact_pending"
    CONTEXT_COMPACTED = "context_compacted"
    ARTIFACT_STORED = "artifact_stored"
    MEMORY_STORED = "memory_stored"
    MEMORY_EXPIRED = "memory_expired"
    SKILL_REGISTERED = "skill_registered"
    SKILL_STATUS_CHANGED = "skill_status_changed"
    CAPABILITY_INJECTED = "capability_injected"
    FEEDBACK_RECORDED = "feedback_recorded"
    SKILL_CANDIDATE_PROPOSED = "skill_candidate_proposed"
    SKILL_CANDIDATE_REJECTED = "skill_candidate_rejected"
    CANDIDATE_EVALUATED = "candidate_evaluated"
    CHAMPION_PROMOTED = "champion_promoted"
    CHAMPION_ROLLED_BACK = "champion_rolled_back"
    MCP_SERVER_CONNECTED = "mcp_server_connected"
    MCP_SERVER_DISCONNECTED = "mcp_server_disconnected"
    MCP_TOOLS_REGISTERED = "mcp_tools_registered"
    SUBAGENT_TASK_STARTED = "subagent_task_started"
    SUBAGENT_TASK_FINISHED = "subagent_task_finished"
    INTENT_CLASSIFIED = "intent_classified"


TERMINAL_OPERATION_EVENTS = frozenset(
    {
        EventType.OPERATION_FINISHED,
        EventType.OPERATION_FAILED,
        EventType.OPERATION_ABORTED,
    }
)

SUSPENDED_STATUS = "suspended"
"""Status an operation, lane and session take while a decision is owed."""


class Payload(BaseModel):
    """Base payload. Unknown keys are kept so old logs stay readable."""

    model_config = ConfigDict(extra="allow")


class SessionCreated(Payload):
    title: str | None = None
    workspace_root: str | None = None


class OperationStarted(Payload):
    name: str | None = None
    deadline_ms: int | None = None


class ModelRequested(Payload):
    provider: str | None = None
    model: str | None = None
    prompt: str | None = None


class AssistantMessage(Payload):
    content: str = ""
    role: str = "assistant"


class ApprovalRequested(Payload):
    approval_id: str
    reason: str | None = None


class ApprovalResolved(Payload):
    approval_id: str
    approved: bool
    reason: str | None = None


class ToolStarted(Payload):
    tool_name: str
    call_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    risk: str | None = None
    """Declared risk level, copied from the manifest so recovery can triage
    an unfinished call without re-consulting the registry."""
    idempotent: bool = False
    """Whether the manifest declares the call safe to run twice. Recovery also
    needs the risk level: an idempotent write still owes a confirmation."""


class ToolResult(Payload):
    tool_name: str
    call_id: str | None = None
    success: bool = True
    output: Any = None
    error: str | None = None


class OperationFinished(Payload):
    result: Any = None


class OperationFailed(Payload):
    error: str
    error_code: str | None = None


class OperationAborted(Payload):
    reason: str | None = None


class OperationSuspended(Payload):
    """A decision is owed before the operation may continue."""

    reason: str
    pending_tool_call_ids: list[str] = Field(default_factory=list)
    detail: str | None = None


class OperationResumed(Payload):
    """Recovery took the operation out of suspended."""

    resumed_from_seq: int | None = None
    confirmed_tool_call_ids: list[str] = Field(default_factory=list)
    replayed_tool_call_ids: list[str] = Field(default_factory=list)


class SnapshotCreated(Payload):
    snapshot_id: str | None = None
    state_hash: str | None = None
    last_seq: int | None = None
    """Last valid seq folded into this snapshot. Recovery replays from here."""
    path: str | None = None
    checksum: str | None = None
    event_count: int | None = None


class LaneCreated(Payload):
    lane: str
    parent_lane: str | None = None
    reason: str | None = None


class BranchCreated(Payload):
    """A new lane forked off an existing one at a known seq."""

    lane: str
    parent_lane: str | None = None
    from_seq: int | None = None
    label: str | None = None


class BranchSwitched(Payload):
    """Navigation only. History is never rewritten or deleted."""

    lane: str
    from_lane: str | None = None
    at_seq: int | None = None


class ModelStreamCompleted(Payload):
    """Summary of one model response. The text itself lives in assistant_message."""

    request_id: str | None = None
    provider: str | None = None
    model: str | None = None
    stop_reason: str | None = None
    iteration: int | None = None
    text_length: int = 0
    tool_call_count: int = 0
    invalid_tool_call_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


class ProviderError(Payload):
    """A provider call failed or its stream broke mid-message."""

    provider: str | None = None
    model: str | None = None
    request_id: str | None = None
    error: str
    error_code: str | None = None
    status_code: int | None = None
    retryable: bool = False
    attempt: int = 1


class QueueMessageEnqueued(Payload):
    queue: str
    message_id: str
    content: str = ""
    source: str = "user"


class QueueMessageConsumed(Payload):
    queue: str
    message_id: str
    iteration: int | None = None


COMPACTION_REASONS = frozenset({"manual", "threshold", "overflow"})
"""Why a compaction ran. Recorded so an operator can tell a deliberate
``/compact`` apart from one the token budget forced."""


class ContextCompactPending(Payload):
    """The soft threshold was crossed. Nothing has been compacted yet.

    Written once per operation when usage first passes the preparation mark, so
    the decision to compact is visible in the log before it happens rather than
    only afterwards.
    """

    used_tokens: int = 0
    limit_tokens: int = 0
    ratio: float = 0.0
    iteration: int | None = None


class ContextCompacted(Payload):
    """One structured compaction. Only the model's context is replaced.

    The original events, diffs and artifacts stay in the log untouched: this
    payload records the summary that took their place in the *prompt*, not a
    deletion. Every field the plan requires is present even when empty, so a
    consumer never has to guess whether a key was dropped or simply had nothing
    in it.
    """

    reason: str = "threshold"
    used_tokens: int = 0
    limit_tokens: int = 0
    ratio: float = 0.0
    freed_tokens: int = 0
    replaced_messages: int = 0
    iteration: int | None = None
    current_objective: str = ""
    task_progress: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    tool_lessons: list[str] = Field(default_factory=list)
    failed_paths: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class ArtifactStored(Payload):
    """A large tool output moved to a file; the context keeps only this reference.

    ``size`` is the artifact's own byte count, not the truncated preview's, so a
    reader can tell how much was set aside.
    """

    artifact_id: str
    kind: str = "tool_output"
    path: str | None = None
    checksum: str | None = None
    size: int = 0
    tool_name: str | None = None
    call_id: str | None = None
    preview: str | None = None


MEMORY_LAYERS = frozenset({"working", "episodic", "semantic", "procedural"})
"""The four layers the plan names. A closed set so retrieval can weight by layer
and an audit can group by it; an unknown layer is refused rather than stored."""

SKILL_STATUSES = frozenset({"draft", "candidate", "active", "deprecated", "retired"})
"""A skill's lifecycle. Only ``active`` may be injected: a candidate that has not
passed evaluation must not become the effective version."""

INJECTABLE_SKILL_STATUSES = frozenset({"active"})
"""Statuses allowed into a prompt. Kept separate from :data:`SKILL_STATUSES` so
widening the lifecycle never silently widens what reaches the model."""

CAPABILITY_KINDS = frozenset({"memory", "skill"})

SKIP_REASONS = frozenset(
    {
        "no_match",
        "below_threshold",
        "not_permitted",
        "expired",
        "not_active",
        "budget",
        "duplicate",
        "over_limit",
    }
)
"""Why a candidate was not injected. The plan requires the trace to explain the
choice, which means the rejections need names as stable as the selections."""


class MemoryStored(Payload):
    """One memory record with the provenance the plan requires.

    ``expires_at_ms`` is what keeps a stale episodic observation from being read
    back as a long-term fact: retrieval compares it against the clock rather than
    trusting the record's age.
    """

    memory_id: str
    layer: str = "working"
    content: str = ""
    source_task: str | None = None
    source_session_id: str | None = None
    created_at_ms: int | None = None
    expires_at_ms: int | None = None
    confidence: float = 0.5
    evidence_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class MemoryExpired(Payload):
    """A memory left the retrievable set. The record itself stays in the log."""

    memory_id: str
    layer: str = "working"
    reason: str = "ttl"
    expired_at_ms: int | None = None


class SkillRegistered(Payload):
    """A skill version became known. Registration is not activation.

    The instruction ``body`` travels in the event rather than being left on disk.
    A skill file can be edited or deleted after the fact, and a log that recorded
    only a path could no longer say what the model was actually told; ``checksum``
    is then a way to notice the file drifted, not the only copy of the text.
    """

    skill_id: str
    version: str = "0.1.0"
    status: str = "draft"
    name: str | None = None
    description: str = ""
    body: str = ""
    source_path: str | None = None
    checksum: str | None = None
    required_scopes: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_task: str | None = None
    registered_at_ms: int | None = None


class SkillStatusChanged(Payload):
    """A lifecycle transition, with the evaluation that justified it."""

    skill_id: str
    version: str = "0.1.0"
    from_status: str = "draft"
    to_status: str = "candidate"
    reason: str | None = None
    evaluation_ref: str | None = None


class CapabilitySelection(Payload):
    """One item that entered the capability slot."""

    kind: str = "memory"
    ref_id: str
    version: str | None = None
    layer: str | None = None
    score: float = 0.0
    tokens: int = 0
    source_task: str | None = None
    source_path: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class CapabilitySkip(Payload):
    """One candidate that did not, and the named reason it did not."""

    kind: str = "memory"
    ref_id: str
    reason: str = "no_match"
    detail: str | None = None


class CapabilityInjected(Payload):
    """What the capability slot held for one model request.

    Both halves are recorded. The selections alone would let an operator see what
    the model read; the skips are what let them answer why something they expected
    is missing, which is the question an audit actually asks.
    """

    query: str = ""
    iteration: int | None = None
    token_budget: int = 0
    tokens_used: int = 0
    granted_scopes: list[str] = Field(default_factory=list)
    selected: list[CapabilitySelection] = Field(default_factory=list)
    skipped: list[CapabilitySkip] = Field(default_factory=list)


FEEDBACK_KINDS = frozenset({"correction", "failure", "success"})
"""Where a candidate may come from. The plan names exactly these three sources, and
keeping them closed is what lets a candidate's provenance be grouped and audited."""

CANDIDATE_DECISIONS = frozenset({"add", "merge", "reject"})
"""What retrieval against the existing skills decided to do with a candidate."""

CANDIDATE_REJECTIONS = frozenset(
    {
        "schema",
        "lint",
        "security",
        "duplicate",
        "no_evidence",
        "low_signal",
    }
)
"""Why a candidate never reached evaluation. Separate from the evaluation verdict:
a candidate refused by the security check was never measured, and an audit that
could not tell those apart would read a refusal as a failing score."""

EVALUATION_STAGES = frozenset({"rules", "judge", "shadow"})
"""The three checks the plan requires, in the order they run. A stage that did not
run is absent rather than recorded as passing."""

EVALUATION_VERDICTS = frozenset({"pass", "fail", "inconclusive"})
"""``inconclusive`` exists because a judge that could not be reached is not a pass.
Promotion requires ``pass``, so an unavailable evaluator blocks rather than waves
a candidate through."""


class EvaluationMetrics(Payload):
    """The seven numbers the plan requires from every evaluation.

    All seven are always present, even at zero: a consumer comparing a candidate
    against the champion must not have to tell a missing key apart from a real
    zero, and a regression is exactly the kind of thing an absent field hides.
    """

    pass_at_1: float = 0.0
    completion_rate: float = 0.0
    tool_effectiveness: float = 0.0
    cost_usd: float = 0.0
    safety_violation_rate: float = 0.0
    regression_rate: float = 0.0
    recovery_rate: float = 0.0


class FeedbackRecorded(Payload):
    """One correction, failure or success worth learning from.

    ``evidence_refs`` is not decoration. The plan requires a candidate to bind to
    its source, and a feedback item with no evidence cannot support one, so this
    is where the binding starts rather than where it is inferred later.
    """

    feedback_id: str
    kind: str = "correction"
    content: str = ""
    source_task: str | None = None
    source_session_id: str | None = None
    tool_name: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at_ms: int | None = None


class SkillCandidateProposed(Payload):
    """A proposed skill version, bound to the feedback that produced it.

    A candidate is a *proposal*, never an effective capability: it is registered at
    ``candidate`` status, and the missing ``draft -> active`` edge plus the
    active-only injection filter are what keep it out of a prompt until an
    evaluation says otherwise.
    """

    candidate_id: str
    skill_id: str
    version: str = "0.1.0"
    decision: str = "add"
    name: str | None = None
    description: str = ""
    body: str = ""
    triggers: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    feedback_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    merged_from: str | None = None
    """Existing skill version this candidate merges into, when ``decision`` is
    ``merge``. Kept so a merge can be told from an unrelated new skill."""
    created_at_ms: int | None = None


class SkillCandidateRejected(Payload):
    """A candidate refused before evaluation, with the check that refused it."""

    candidate_id: str
    skill_id: str | None = None
    reason: str = "schema"
    detail: str | None = None


class CandidateEvaluated(Payload):
    """One evaluation of one candidate against the fixed task set.

    The champion's own numbers travel alongside the candidate's. A verdict on its
    own cannot answer whether a passing candidate is actually better than what is
    already serving requests, and that comparison is the promotion decision.
    """

    evaluation_id: str
    candidate_id: str
    skill_id: str
    version: str = "0.1.0"
    dataset: str = ""
    verdict: str = "fail"
    stages: list[str] = Field(default_factory=list)
    failed_stages: list[str] = Field(default_factory=list)
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)
    baseline_metrics: EvaluationMetrics | None = None
    champion_version: str | None = None
    task_count: int = 0
    failures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    evaluated_at_ms: int | None = None


class ChampionPromoted(Payload):
    """A candidate became the effective version, naming the evaluation that let it.

    ``from_version`` is the version being displaced, and it is deprecated rather
    than retired: a rollback needs somewhere to roll back to.
    """

    skill_id: str
    to_version: str
    from_version: str | None = None
    candidate_id: str | None = None
    evaluation_id: str | None = None
    reason: str | None = None


class ChampionRolledBack(Payload):
    """The effective version moved back to a named earlier version.

    ``to_version`` is explicit rather than "the previous one": inferring it from
    history would make the outcome depend on how many promotions happened since,
    and a rollback is the one operation that must land somewhere known.
    """

    skill_id: str
    to_version: str
    from_version: str | None = None
    reason: str | None = None
    evaluation_id: str | None = None


MCP_TRANSPORTS = frozenset({"stdio", "http"})
"""How a server is reached. Closed because the isolation argument differs per
transport: a stdio server is a child process this runtime owns, an http one is a
remote endpoint that has to clear the network policy first."""

MCP_DISCONNECT_REASONS = frozenset({"shutdown", "timeout", "handshake_failed", "transport_error"})
"""Why a connection ended. ``shutdown`` is the orderly close; the other three are
failures, and keeping them apart is what lets an operator tell a server that was
never reachable from one that stopped answering mid-session."""

SUBAGENT_OUTCOMES = frozenset({"completed", "timeout", "budget_exceeded", "failed", "denied"})
"""How a sub-agent task ended. ``denied`` covers a task refused before it ran --
an allowed-tools set the parent cannot delegate, for instance -- which is not the
same as one that ran and failed."""

SUBAGENT_RETURN_FORMATS = frozenset({"text", "json"})
"""What the parent asked the child to return. Declared per task rather than
inferred from the answer, so a malformed reply is a task failure rather than a
silently reinterpreted result."""


class McpServerConnected(Payload):
    """A configured MCP server completed its handshake.

    The tool count and the capability list are recorded rather than derived at read
    time: a server can change what it advertises between sessions, and an audit of
    what the runtime *had* available must not depend on asking it again now.
    """

    server: str
    transport: str = "stdio"
    address: str | None = None
    """Command line for stdio, base URL for http. Credentials never appear here."""
    protocol_version: str | None = None
    tool_count: int = 0
    capabilities: list[str] = Field(default_factory=list)
    connected_at_ms: int | None = None


class McpServerDisconnected(Payload):
    """A connection ended, orderly or otherwise."""

    server: str
    reason: str = "shutdown"
    detail: str | None = None
    duration_ms: int | None = None


class McpToolsRegistered(Payload):
    """External tools were admitted into the one registry, under vetted manifests.

    ``rejected`` is as load-bearing as ``tools``: a server offering a tool this
    runtime refuses to bridge is the ordinary case, not an error, and an operator
    needs to see which name was dropped and why rather than wonder where it went.
    """

    server: str
    tools: list[str] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    granted_scopes: list[str] = Field(default_factory=list)
    """Scopes the bridged manifests declare. The policy still enforces the grant;
    this records what the bridge asked for so the two can be compared."""


class SubagentTaskStarted(Payload):
    """A child task was dispatched with an explicit, narrower contract.

    Every limit is written down before the child runs. A sub-agent whose budget
    was decided while it executed cannot be audited, and the parent's own limits
    are not a contract the child agreed to.
    """

    task_id: str
    child_session_id: str
    objective: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    max_tokens: int | None = None
    deadline_ms: int | None = None
    return_format: str = "text"
    parent_operation_id: str | None = None


class SubagentTaskFinished(Payload):
    """A child task ended, with its result and the evidence behind it.

    ``evidence_refs`` points at the child's own events. The child's session is not
    merged into the parent's, so this reference is the only durable link between a
    returned answer and the work that produced it.
    """

    task_id: str
    child_session_id: str
    outcome: str = "completed"
    result: str = ""
    error: str | None = None
    error_code: str | None = None
    tool_calls: int = 0
    total_tokens: int = 0
    duration_ms: int | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class IntentCandidateRecord(Payload):
    """One label a classifier scored, attributed to the classifier that scored it.

    The attribution is what makes a fused conclusion auditable: two routes agreeing
    and one route deciding alone produce the same winning label, and only the
    per-classifier rows tell them apart.
    """

    label: str
    score: float = 0.0
    classifier: str = ""


class IntentClassified(Payload):
    """A recorded intent signal for one user input.

    Written whether or not the classification reached a usable conclusion. An
    abstention is the ordinary case rather than an error, and a log that only kept
    the confident answers could not say how often the taxonomy fails to cover what
    users actually ask -- which is the question the signal exists to answer.

    ``classifiers_abstained`` is kept apart from ``degraded`` on purpose. A route
    that ran and found its evidence too thin is not a route that failed to run, and
    collapsing the two would make ordinary short inputs look like a broken runtime.
    """

    query: str
    intent: str
    taxonomy_version: str
    query_hash: str = ""
    """Stable hash of the input, so a replay can assert the record it read belongs
    to the input being replayed rather than trusting position in the log."""

    confidence: float = 0.0
    margin: float = 0.0
    """First place minus second place. Recorded separately from ``candidates``
    because agreement drives ``confidence`` to 1.0 and says nothing; the margin is
    what shows two labels could not be told apart."""

    abstained: bool = False
    abstain_reason: str | None = None
    candidates: list[IntentCandidateRecord] = Field(default_factory=list)
    classifiers_configured: list[str] = Field(default_factory=list)
    classifiers_run: list[str] = Field(default_factory=list)
    """Configured, started and finished without error. A route that ran and then
    failed its own evidence gate still counts as run."""

    classifiers_abstained: list[str] = Field(default_factory=list)
    """Ran but produced no candidate. Classifier names, not reasons: the name
    already determines which gate was missed."""

    degraded: bool = False
    degraded_reason: str | None = None
    model_called: bool = False
    duration_ms: int = 0
    iteration: int | None = None
    """Which iteration the input arrived on, not which iteration the signal serves.
    One classification serves every iteration until the next user input."""


PAYLOAD_TYPES: dict[EventType, type[Payload]] = {
    EventType.SESSION_CREATED: SessionCreated,
    EventType.OPERATION_STARTED: OperationStarted,
    EventType.MODEL_REQUESTED: ModelRequested,
    EventType.ASSISTANT_MESSAGE: AssistantMessage,
    EventType.APPROVAL_REQUESTED: ApprovalRequested,
    EventType.APPROVAL_RESOLVED: ApprovalResolved,
    EventType.TOOL_STARTED: ToolStarted,
    EventType.TOOL_RESULT: ToolResult,
    EventType.OPERATION_FINISHED: OperationFinished,
    EventType.OPERATION_FAILED: OperationFailed,
    EventType.OPERATION_ABORTED: OperationAborted,
    EventType.OPERATION_SUSPENDED: OperationSuspended,
    EventType.OPERATION_RESUMED: OperationResumed,
    EventType.SNAPSHOT_CREATED: SnapshotCreated,
    EventType.LANE_CREATED: LaneCreated,
    EventType.BRANCH_CREATED: BranchCreated,
    EventType.BRANCH_SWITCHED: BranchSwitched,
    EventType.MODEL_STREAM_COMPLETED: ModelStreamCompleted,
    EventType.PROVIDER_ERROR: ProviderError,
    EventType.QUEUE_MESSAGE_ENQUEUED: QueueMessageEnqueued,
    EventType.QUEUE_MESSAGE_CONSUMED: QueueMessageConsumed,
    EventType.CONTEXT_COMPACT_PENDING: ContextCompactPending,
    EventType.CONTEXT_COMPACTED: ContextCompacted,
    EventType.ARTIFACT_STORED: ArtifactStored,
    EventType.MEMORY_STORED: MemoryStored,
    EventType.MEMORY_EXPIRED: MemoryExpired,
    EventType.SKILL_REGISTERED: SkillRegistered,
    EventType.SKILL_STATUS_CHANGED: SkillStatusChanged,
    EventType.CAPABILITY_INJECTED: CapabilityInjected,
    EventType.FEEDBACK_RECORDED: FeedbackRecorded,
    EventType.SKILL_CANDIDATE_PROPOSED: SkillCandidateProposed,
    EventType.SKILL_CANDIDATE_REJECTED: SkillCandidateRejected,
    EventType.CANDIDATE_EVALUATED: CandidateEvaluated,
    EventType.CHAMPION_PROMOTED: ChampionPromoted,
    EventType.CHAMPION_ROLLED_BACK: ChampionRolledBack,
    EventType.MCP_SERVER_CONNECTED: McpServerConnected,
    EventType.MCP_SERVER_DISCONNECTED: McpServerDisconnected,
    EventType.MCP_TOOLS_REGISTERED: McpToolsRegistered,
    EventType.SUBAGENT_TASK_STARTED: SubagentTaskStarted,
    EventType.SUBAGENT_TASK_FINISHED: SubagentTaskFinished,
    EventType.INTENT_CLASSIFIED: IntentClassified,
}


class Event(BaseModel):
    """The immutable envelope persisted to JSONL and indexed in SQLite."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    CURRENT_SCHEMA_VERSION: ClassVar[int] = CURRENT_SCHEMA_VERSION

    schema_version: int = CURRENT_SCHEMA_VERSION
    event_id: str = Field(min_length=1)
    event_type: EventType
    session_id: str = Field(min_length=1)
    seq: int = Field(gt=0)
    timestamp_ms: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1)
    lane_id: str = Field(default=DEFAULT_LANE, min_length=1)
    operation_id: str | None = None
    payload: SerializeAsAny[Payload]

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        version = values.get("schema_version", CURRENT_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise EventValidationError(
                "unsupported event schema version",
                details={
                    "schema_version": version,
                    "supported": sorted(SUPPORTED_SCHEMA_VERSIONS),
                },
            )
        raw_type = values.get("event_type", values.get("type"))
        if "type" in values:
            values = {key: value for key, value in values.items() if key != "type"}
            if raw_type is not None:
                values["event_type"] = raw_type
        if not isinstance(raw_type, str):
            return values
        try:
            event_type = EventType(raw_type)
        except ValueError:
            return values
        payload_type = PAYLOAD_TYPES[event_type]
        payload = values.get("payload") or {}
        if not isinstance(payload, payload_type):
            values = {**values, "payload": payload_for_event(event_type, payload)}
        return values

    @property
    def type(self) -> EventType:
        """Compatibility alias useful when reading event streams."""

        return self.event_type

    @classmethod
    def create(
        cls,
        event_type: EventType,
        *,
        session_id: str,
        seq: int,
        payload: Payload | dict[str, Any] | None = None,
        factory: IdFactory | None = None,
        lane_id: str = DEFAULT_LANE,
        operation_id: str | None = None,
        idempotency_key_value: str | None = None,
    ) -> Event:
        factory = factory or IdFactory()
        payload_type = PAYLOAD_TYPES[event_type]
        typed_payload = (
            payload
            if isinstance(payload, payload_type)
            else payload_for_event(event_type, payload or {})
        )
        key = idempotency_key_value or factory.idempotency_key(
            session_id,
            lane_id,
            seq,
            event_type.value,
        )
        return cls(
            event_id=factory.event_id(),
            event_type=event_type,
            session_id=session_id,
            seq=seq,
            timestamp_ms=factory.timestamp_ms(),
            idempotency_key=key,
            lane_id=lane_id,
            operation_id=operation_id,
            payload=typed_payload,
        )

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def payload_for_event(event_type: EventType, payload: Payload | dict[str, Any]) -> Payload:
    """Validate a payload and expose a domain error instead of Pydantic internals."""

    try:
        return PAYLOAD_TYPES[event_type].model_validate(payload)
    except Exception as exc:
        raise EventValidationError(
            f"invalid payload for {event_type.value}",
            details={"error": str(exc)},
        ) from exc
