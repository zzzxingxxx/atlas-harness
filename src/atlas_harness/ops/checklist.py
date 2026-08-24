"""The release checklist and the risk register, both computed from real state.

M9's completion condition is 发布检查清单全部通过，关键风险有明确的监控、暂停和回滚动作
-- every checklist item passes, and every key risk has a named monitor, pause and
rollback. A checklist whose items are hardcoded ``True`` satisfies the letter of
that and none of the point, so every item here is computed: the schema policy is
re-derived, the data directory is verified, a backup is taken and restored into a
scratch directory, the frozen samples are folded and compared to hashes committed
months ago, and the logs and exports are scanned for anything that looks like a
credential.

The risk register is the other half. It is data rather than prose because the plan
asks for three specific actions per risk -- what watches for it, what stops the
system when it fires, and what undoes it -- and a paragraph of prose can omit one
without anybody noticing. :func:`check_risk_register` fails the release if a risk
is missing an action, and every entry cites the code or command that implements it
so a reviewer can check the claim rather than trust it.

Nothing in here writes to the data directory it is checking. The backup round trip
uses a temporary directory and removes it, so a release check is safe to run
against production -- which is the only way it will actually get run.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atlas_harness import __version__
from atlas_harness.events import compat
from atlas_harness.events.models import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    Event,
    EventType,
)
from atlas_harness.events.reducer import replay
from atlas_harness.events.store import LOG_FILENAME, SESSIONS_DIRNAME, EventStore
from atlas_harness.kernel.errors import AtlasError, PolicyDeniedError
from atlas_harness.observability.export import build_bundle
from atlas_harness.ops.backup import create_backup, restore_backup, verify_backup
from atlas_harness.ops.verify import verify_data_dir
from atlas_harness.policy.command_policy import CommandPolicy
from atlas_harness.policy.network_policy import NetworkPolicy
from atlas_harness.policy.path_policy import PathPolicy
from atlas_harness.tools.manifest import RiskLevel
from atlas_harness.tools.redaction import redact

SAMPLE_EXPECTATIONS_FILENAME = "expected.json"
DEMO_SESSION_ID = "ses_release_demo"

CHECK_NAMES: tuple[str, ...] = (
    "schema_policy",
    "data_dir_verified",
    "backup_round_trip",
    "sample_replay",
    "schema_coverage",
    "demo_session",
    "no_secrets_in_output",
    "side_effect_tools_gated",
    "policy_denies_the_obvious",
    "risk_register",
)
"""The closed list, in the order a release reads them. A caller may gate on a
name, so a new check is added here rather than invented in a message."""


class CheckResult(BaseModel):
    """One checklist item, its verdict, and the evidence behind the verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    title: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    """Numbers and names the verdict was derived from. Deliberately never raw
    matched text: this object is printed, and a secrets check that echoed what it
    found would leak the secret it exists to catch."""

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def render(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        suffix = f" -- {self.detail}" if self.detail else ""
        return f"[{mark}] {self.title}{suffix}"


class RiskControl(BaseModel):
    """One plan risk with the three actions the plan requires for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    risk: str
    signal: str
    monitor: str
    pause: str
    rollback: str
    evidence: tuple[str, ...] = ()
    """Where the three actions are implemented. A control nobody can point at is
    an intention, and the register exists to tell those apart."""

    @property
    def complete(self) -> bool:
        return bool(self.monitor and self.pause and self.rollback and self.evidence)

    def as_json(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["complete"] = self.complete
        return payload

    def render(self) -> list[str]:
        return [
            f"{self.risk}",
            f"  signal:   {self.signal}",
            f"  monitor:  {self.monitor}",
            f"  pause:    {self.pause}",
            f"  rollback: {self.rollback}",
            f"  where:    {', '.join(self.evidence)}",
        ]


RISK_REGISTER: tuple[RiskControl, ...] = (
    RiskControl(
        risk="prompt injection",
        signal="a tool result asks for more permission or for an extra command",
        monitor="tool_result events carry untrusted content; the audit log's tool "
        "category and `atlas audit` surface every one",
        pause="the injected call re-enters the policy engine and a write, network or "
        "destructive tool raises approval_requested rather than running",
        rollback="deny the approval; the operation records approval_resolved "
        "approved=false and finishes without the side effect",
        evidence=(
            "policy/engine.py",
            "tools/redaction.py",
            "tests/security/test_prompt_injection.py",
        ),
    ),
    RiskControl(
        risk="replay side effect",
        signal="recovery finds a tool_started with no tool_result",
        monitor="`atlas doctor` and `atlas recover` report unfinished operations; "
        "the replay report lists them as problems",
        pause="the operation is suspended and resume refuses to re-run a "
        "non-idempotent call without an explicit --confirm",
        rollback="`atlas abort` closes the operation with operation_aborted, leaving "
        "the log intact for an audit",
        evidence=(
            "session/service.py",
            "transport/cli.py:recover",
            "tests/integration/test_crash_recovery.py",
        ),
    ),
    RiskControl(
        risk="context loss after compaction",
        signal="a compacted session can no longer name the file or the failure it was working on",
        monitor="context_compacted records replaced_messages and evidence refs; "
        "`atlas trace` shows what was dropped",
        pause="compaction is proposed as context_compact_pending and the raw events "
        "are never deleted",
        rollback="rebuild the context from the log, which still holds every original message",
        evidence=(
            "context/compaction.py",
            "events/reducer.py",
            "tests/unit/test_context_compaction.py",
        ),
    ),
    RiskControl(
        risk="skill poisoning",
        signal="a candidate skill only helps the one task that proposed it",
        monitor="candidate_evaluated records the benchmark score per candidate",
        pause="the pending window holds a candidate out of the prompt until it is promoted",
        rollback="`atlas skill-rollback` writes champion_rolled_back and restores the "
        "previous version",
        evidence=(
            "evolution/pipeline.py",
            "transport/cli.py:skill-rollback",
            "tests/unit/test_evolution.py",
        ),
    ),
    RiskControl(
        risk="sqlite and jsonl disagree",
        signal="the index's last seq is not the log's last seq",
        monitor="`atlas verify` reports index drift as repairable findings; the store "
        "reconciles and logs on open",
        pause="the log is the only source of truth, so a stale index never changes an "
        "answer -- reads fold the log",
        rollback="`atlas reindex` deletes the index rows and rebuilds them from the "
        "log in one transaction",
        evidence=(
            "ops/verify.py",
            "ops/migrate.py",
            "tests/integration/test_store_recovery.py",
        ),
    ),
    RiskControl(
        risk="provider instability",
        signal="a stream disconnects, rate limits, or returns an unexpected shape",
        monitor="provider_error events and the metrics file's provider_errors counter",
        pause="the adapter's timeout and retry budget stop the loop instead of "
        "hammering the provider",
        rollback="the operation fails with operation_failed; the session is resumable "
        "because nothing partial was committed",
        evidence=(
            "model/protocol.py",
            "agent/loop.py",
            "tests/unit/test_model_fake_provider.py",
        ),
    ),
    RiskControl(
        risk="excessive concurrency",
        signal="tool results arrive in an unstable order or read half-written state",
        monitor="the trace's per-call ordering and the parallel_safe flag on every manifest",
        pause="only read-risk tools run in parallel; anything with a side effect "
        "serializes in the lane",
        rollback="re-run the operation; reads have no side effect to undo",
        evidence=(
            "tools/manifest.py",
            "agent/loop.py",
            "tests/unit/test_tool_registry.py",
        ),
    ),
    RiskControl(
        risk="scope creep",
        signal="platform work starts before the current milestone's gate passes",
        monitor="the five-command gate and this checklist run per pull request",
        pause="a milestone does not ship until its gate is green",
        rollback="revert the commit; every milestone is a separate commit on main",
        evidence=("ops/checklist.py", "README.md"),
    ),
)
"""The plan's section 15 register, with the three required actions filled in from
what the code actually does. Frozen alongside the schema history: a risk is edited
when its control changes, not when a release is inconvenient."""


class ReleaseReport(BaseModel):
    """The checklist and the register as one object a release gate can read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    atlas_version: str = __version__
    schema_version: int = CURRENT_SCHEMA_VERSION
    data_dir: str = ""
    checks: tuple[CheckResult, ...] = ()
    risks: tuple[RiskControl, ...] = ()

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if not check.passed)

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_json(self) -> dict[str, Any]:
        return {
            "atlas_version": self.atlas_version,
            "schema_version": self.schema_version,
            "data_dir": self.data_dir,
            "ok": self.ok,
            "passed": len(self.checks) - len(self.failed),
            "total": len(self.checks),
            "checks": [check.as_json() for check in self.checks],
            "risks": [risk.as_json() for risk in self.risks],
        }

    def render(self) -> list[str]:
        lines = [
            f"atlas {self.atlas_version}, schema v{self.schema_version}",
            f"data dir: {self.data_dir}",
            "",
            "release checklist:",
        ]
        lines.extend(f"  {check.render()}" for check in self.checks)
        lines.extend(["", "risk register:"])
        for risk in self.risks:
            lines.extend(f"  {line}" for line in risk.render())
        passed = len(self.checks) - len(self.failed)
        lines.extend(
            [
                "",
                f"verdict: {'ready' if self.ok else 'not ready'} "
                f"({passed}/{len(self.checks)} checks passed)",
            ]
        )
        return lines


def check_schema_policy() -> CheckResult:
    """The frozen compatibility policy still describes the code."""

    findings = compat.check_policy()
    return CheckResult(
        name="schema_policy",
        title="event schema history matches the build",
        passed=not findings,
        detail="; ".join(findings),
        evidence={
            "current_schema_version": CURRENT_SCHEMA_VERSION,
            "supported": sorted(SUPPORTED_SCHEMA_VERSIONS),
            "event_types": len(EventType),
        },
    )


def check_data_dir(store: EventStore) -> CheckResult:
    """Every log parses, every seq is contiguous, every artifact still hashes."""

    report = verify_data_dir(store)
    counts = report.counts()
    return CheckResult(
        name="data_dir_verified",
        title="logs, index and artifacts verify",
        passed=report.ok,
        detail="" if report.ok else "; ".join(item.render() for item in report.all_findings[:5]),
        evidence={"sessions": len(report.sessions), **counts},
    )


def check_backup_round_trip(store: EventStore) -> CheckResult:
    """Back up, verify the copy, restore it, and fold the restored logs.

    Run against a scratch directory rather than a fixture because the thing worth
    knowing before a release is whether *this* data directory round trips. A
    backup tool proven only on synthetic data is proven on the wrong data.
    """

    sessions = store.list_session_ids()
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-release-") as scratch:
            root = Path(scratch)
            manifest = create_backup(store, root / "backup")
            verification = verify_backup(root / "backup")
            if not verification.ok:
                return CheckResult(
                    name="backup_round_trip",
                    title="backup verifies and restores to the same state",
                    passed=False,
                    detail="the backup failed its own checksums",
                    evidence={
                        "missing": list(verification.missing),
                        "corrupt": list(verification.corrupt),
                    },
                )
            restored = restore_backup(root / "backup", root / "restored")
    except AtlasError as error:
        return CheckResult(
            name="backup_round_trip",
            title="backup verifies and restores to the same state",
            passed=False,
            detail=f"{error.code}: {error.message}",
            evidence={"sessions": len(sessions)},
        )

    mismatched = [entry.session_id for entry in restored.sessions if not entry.matches]
    return CheckResult(
        name="backup_round_trip",
        title="backup verifies and restores to the same state",
        passed=restored.compatible and len(restored.sessions) == len(sessions),
        detail="" if restored.compatible else f"state hash changed for {mismatched}",
        evidence={
            "sessions": len(restored.sessions),
            "files": len(manifest.files()),
            "bytes": sum(item.size for item in manifest.files()),
            "notes": list(restored.notes),
        },
    )


def _read_log(path: Path) -> list[Event]:
    """Parse one sample log without opening a store beside it.

    Reading the file directly keeps a release check from creating an index inside
    the committed sample directory, which would make the working tree dirty every
    time the checklist runs.
    """

    events: list[Event] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(Event.model_validate(json.loads(line)))
    return events


def _sample_expectations(samples_dir: Path) -> dict[str, Any]:
    path = samples_dir / SAMPLE_EXPECTATIONS_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("sample expectations are not an object")
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def check_sample_replay(samples_dir: Path) -> CheckResult:
    """The frozen sample sessions still fold to the hashes committed with them.

    This is the plan's 使用固定模型参数和固定数据集生成基线报告, and the only check
    here that can fail because of a change made deliberately. That is the point: a
    reducer change that alters a historical projection has to be a decision, and
    regenerating these hashes instead of explaining them deletes the guarantee.
    """

    expectations = {}
    try:
        expectations = _sample_expectations(samples_dir)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return CheckResult(
            name="sample_replay",
            title="frozen sample sessions replay to their recorded hashes",
            passed=False,
            detail=f"cannot read {samples_dir / SAMPLE_EXPECTATIONS_FILENAME}: {error}",
        )

    mismatched: list[str] = []
    checked = 0
    for session_id, expected in sorted(expectations.items()):
        log = samples_dir / SESSIONS_DIRNAME / session_id / LOG_FILENAME
        if not log.exists():
            mismatched.append(f"{session_id}: no log")
            continue
        try:
            events = _read_log(log)
            state = replay(events, session_id=session_id)
        except (AtlasError, ValueError, json.JSONDecodeError) as error:
            mismatched.append(f"{session_id}: {error}")
            continue
        checked += 1
        if state.state_hash() != expected.get("state_hash"):
            mismatched.append(f"{session_id}: {state.state_hash()}")
        elif len(events) != expected.get("events"):
            mismatched.append(f"{session_id}: {len(events)} events")

    return CheckResult(
        name="sample_replay",
        title="frozen sample sessions replay to their recorded hashes",
        passed=bool(expectations) and not mismatched,
        detail="; ".join(mismatched),
        evidence={"sessions": len(expectations), "replayed": checked},
    )


def check_schema_coverage(samples_dir: Path) -> CheckResult:
    """There is a sample log for every schema version this build claims to read.

    A build that says it reads seven versions and only has fixtures for the current
    one is claiming backward compatibility it has never once exercised.
    """

    covered: set[int] = set()
    try:
        expectations = _sample_expectations(samples_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        expectations = {}
    for expected in expectations.values():
        version = expected.get("schema_version")
        if isinstance(version, int):
            covered.add(version)

    missing = sorted(set(SUPPORTED_SCHEMA_VERSIONS) - covered)
    return CheckResult(
        name="schema_coverage",
        title="every supported schema version has a replayable sample",
        passed=not missing,
        detail="" if not missing else f"no sample at schema v{missing}",
        evidence={"covered": sorted(covered), "supported": sorted(SUPPORTED_SCHEMA_VERSIONS)},
    )


def check_demo_session(samples_dir: Path) -> CheckResult:
    """The committed demo shows the three things the plan asks a demo to show.

    Section 13 wants one approval, one truncated or externalized tool result, and
    one test failure followed by a fix that verifies. Checked against the log rather
    than asserted in a document, so the demo cannot rot into a description of a
    session that no longer does any of it.
    """

    log = samples_dir / SESSIONS_DIRNAME / DEMO_SESSION_ID / LOG_FILENAME
    if not log.exists():
        return CheckResult(
            name="demo_session",
            title="the replayable demo covers approval, externalization and a fix",
            passed=False,
            detail=f"no demo log at {log}",
        )

    try:
        events = _read_log(log)
    except (AtlasError, ValueError, json.JSONDecodeError) as error:
        return CheckResult(
            name="demo_session",
            title="the replayable demo covers approval, externalization and a fix",
            passed=False,
            detail=str(error),
        )

    approvals = [event for event in events if event.event_type is EventType.APPROVAL_RESOLVED]
    artifacts = [event for event in events if event.event_type is EventType.ARTIFACT_STORED]
    results = [event for event in events if event.event_type is EventType.TOOL_RESULT]
    outcomes = [
        bool(event.payload.model_dump(mode="python").get("success", True)) for event in results
    ]
    recovered = any(
        not outcomes[index] and any(outcomes[index + 1 :]) for index in range(len(outcomes))
    )

    missing: list[str] = []
    if not approvals:
        missing.append("no approval was resolved")
    if not artifacts:
        missing.append("no tool result was externalized")
    if not recovered:
        missing.append("no failure was followed by a passing run")

    return CheckResult(
        name="demo_session",
        title="the replayable demo covers approval, externalization and a fix",
        passed=not missing,
        detail="; ".join(missing),
        evidence={
            "session_id": DEMO_SESSION_ID,
            "events": len(events),
            "approvals": len(approvals),
            "artifacts": len(artifacts),
            "tool_results": len(results),
        },
    )


def check_no_secrets(store: EventStore) -> CheckResult:
    """No log line, trace, audit record or metrics file looks like a credential.

    The plan's wording is 检查日志、trace、prompt 和异常中没有 API key、Cookie、
    私钥或完整敏感文件, and the check reuses :func:`redact` rather than a second set
    of patterns: if redaction would change a byte of the output, then something that
    the redactor recognises as a secret reached the log without passing through it.

    Locations are reported and matched text never is. A finding names the session and
    the file; reading the secret out of the checklist's own output would defeat it.
    """

    hits: list[str] = []
    scanned = 0
    for session_id in store.list_session_ids():
        log = store.log_path(session_id)
        if log.exists():
            scanned += 1
            for number, line in enumerate(log.read_text(encoding="utf-8").splitlines(), start=1):
                if line and redact(line) != line:
                    hits.append(f"{session_id}:{LOG_FILENAME}:{number}")
        try:
            events = store.read_events(session_id)
        except AtlasError:
            continue
        for filename, body in build_bundle(events, session_id=session_id).files().items():
            scanned += 1
            if body and redact(body) != body:
                hits.append(f"{session_id}:{filename}")

    return CheckResult(
        name="no_secrets_in_output",
        title="no credential-shaped text in logs, traces, audit or metrics",
        passed=not hits,
        detail="" if not hits else f"{len(hits)} location(s): {hits[:5]}",
        evidence={"files_scanned": scanned, "hits": len(hits)},
    )


def check_side_effect_tools() -> CheckResult:
    """Every tool with a side effect needs approval and runs serially."""

    from atlas_harness.tools.registry import default_registry

    problems: list[str] = []
    manifests = default_registry().manifests()
    for manifest in manifests:
        if manifest.risk is RiskLevel.READ:
            continue
        if not manifest.approval_required:
            problems.append(f"{manifest.reference()} runs without approval")
        if manifest.can_run_in_parallel:
            problems.append(f"{manifest.reference()} may run in parallel")

    return CheckResult(
        name="side_effect_tools_gated",
        title="side-effecting tools require approval and do not run in parallel",
        passed=not problems,
        detail="; ".join(problems),
        evidence={
            "tools": len(manifests),
            "side_effecting": sum(1 for item in manifests if item.risk is not RiskLevel.READ),
        },
    )


def check_policy_probes(workspace_root: Path) -> CheckResult:
    """The path, command and network policies still refuse the obvious attacks.

    Six probes, not a test suite -- the suites in ``tests/security`` are exhaustive.
    What this adds is that the refusals are re-confirmed on the machine and in the
    configuration that is about to ship, where a stray environment variable or an
    edited default could have widened them.
    """

    paths = PathPolicy(workspace_root)
    commands = CommandPolicy()
    network = NetworkPolicy()

    probes: tuple[tuple[str, Any], ...] = (
        ("escape the workspace", lambda: paths.resolve_read("../../etc/passwd")),
        ("read a dotenv file", lambda: paths.resolve_read(".env")),
        ("read an ssh key", lambda: paths.resolve_read(".ssh/id_rsa")),
        ("run a denied program", lambda: commands.parse("bash -c whoami")),
        ("inject a second command", lambda: commands.parse("git status; whoami")),
        ("force a destructive flag", lambda: commands.parse("git clean -rf")),
        ("reach the network by default", lambda: network.check("https://example.com")),
    )

    allowed: list[str] = []
    for label, probe in probes:
        try:
            probe()
        except PolicyDeniedError:
            continue
        except AtlasError:
            # Refused for a different reason, which is still a refusal.
            continue
        allowed.append(label)

    return CheckResult(
        name="policy_denies_the_obvious",
        title="path, command and network policies refuse known-bad input",
        passed=not allowed,
        detail="" if not allowed else f"permitted: {allowed}",
        evidence={"probes": len(probes), "workspace_root": str(workspace_root)},
    )


def check_risk_register(register: Sequence[RiskControl] = RISK_REGISTER) -> CheckResult:
    """Every key risk names a monitor, a pause and a rollback."""

    incomplete = [item.risk for item in register if not item.complete]
    return CheckResult(
        name="risk_register",
        title="every key risk has a monitor, a pause and a rollback",
        passed=bool(register) and not incomplete,
        detail="" if not incomplete else f"incomplete: {incomplete}",
        evidence={"risks": len(register)},
    )


def run_release_checks(
    store: EventStore,
    *,
    samples_dir: Path,
    workspace_root: Path | None = None,
) -> ReleaseReport:
    """Run the whole checklist against one data directory and sample set."""

    workspace = workspace_root or Path.cwd()
    checks = (
        check_schema_policy(),
        check_data_dir(store),
        check_backup_round_trip(store),
        check_sample_replay(samples_dir),
        check_schema_coverage(samples_dir),
        check_demo_session(samples_dir),
        check_no_secrets(store),
        check_side_effect_tools(),
        check_policy_probes(workspace),
        check_risk_register(),
    )
    return ReleaseReport(
        data_dir=str(store.data_dir),
        checks=checks,
        risks=RISK_REGISTER,
    )
