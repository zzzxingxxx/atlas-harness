"""Running an evaluation task through a real session.

The evaluator takes a runner as a parameter so tests can script one, but something
has to drive the real thing, and this is it. Each task runs in its own session: the
tasks are independent by construction, and sharing a session would let task three
answer from what task one happened to leave in the transcript, which would make the
set measure history rather than the skill.

The candidate is made available by pointing the service at a *view* of the skill
library whose active set is exactly that one version. Nothing is written: activating
the candidate for real would be a promotion, and the point of a shadow run is that it
happens before one. A crash mid-run therefore cannot leave a candidate live.
"""

from __future__ import annotations

from atlas_harness.agent.service import AgentService
from atlas_harness.evals.datasets import EvalTask
from atlas_harness.evals.replay import RunTrace, trace_of
from atlas_harness.events.store import EventStore
from atlas_harness.skills.models import SkillRecord, SkillStatus
from atlas_harness.skills.repository import SkillRepository


class ForcedSkillView(SkillRepository):
    """A skill library that reports exactly one version as effective.

    Subclassing rather than patching the real repository: the selector reads
    ``active`` and ``search``, and a view that answers those two questions differently
    is a smaller lie than a repository whose stored rows say something else. Writes
    still go through to the underlying store, so nothing here can lose an event.
    """

    def __init__(self, store: EventStore, record: SkillRecord) -> None:
        super().__init__(store)
        self.forced = record.model_copy(update={"status": SkillStatus.ACTIVE})

    def active(self) -> list[SkillRecord]:
        return [self.forced]

    def search(self, query: str, *, limit: int = 10) -> list[tuple[SkillRecord, float]]:
        """The forced version, scored as a match, whatever the query says.

        The real search ranks against FTS rows the candidate may not have yet, and a
        candidate that scored zero would never be injected -- the evaluation would then
        measure the harness without the skill and report it as the skill's result.
        """

        return [(self.forced, 1.0)]


class SessionTaskRunner:
    """Drives one task per session and reads the outcome back off the log."""

    def __init__(self, service: AgentService, *, session_prefix: str = "eval") -> None:
        self.service = service
        self.session_prefix = session_prefix
        self.sessions: list[str] = []

    def __call__(self, task: EvalTask, skill: SkillRecord | None) -> RunTrace:
        session_id = self.service.ensure_session(
            None, title=f"{self.session_prefix}:{task.task_id}"
        )
        self.sessions.append(session_id)
        original = self.service.skills
        if skill is not None:
            self.service.skills = ForcedSkillView(self.service.store, skill)
        try:
            report = self.service.run_sync(task.prompt, session_id=session_id)
        finally:
            self.service.skills = original
        return trace_of(
            self.service.store.read_events(session_id),
            operation_id=report.result.operation_id,
        )
