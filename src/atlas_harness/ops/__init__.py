"""Operations tooling: verify a data directory, back it up, rebuild it, release it.

Everything in here exists because the log is the only source of truth and every
other file on disk is derived from it. That is a strong guarantee and a fragile
one -- it holds only if somebody can prove the log is intact, copy it somewhere
safe, and rebuild the derived state when it drifts. Those three jobs are
:mod:`~atlas_harness.ops.verify`, :mod:`~atlas_harness.ops.backup` and
:mod:`~atlas_harness.ops.migrate`, and :mod:`~atlas_harness.ops.checklist` is the
release gate that runs all of them against real state.

The division of labour between verify and migrate is deliberate: verify reports and
never repairs, so it is safe to run against production, and migrate repairs and
never guesses, so a rebuild is always the log's version of events.
"""

from atlas_harness.ops.backup import (
    MANIFEST_FILENAME,
    BackupManifest,
    BackupVerification,
    RestoreReport,
    create_backup,
    read_manifest,
    restore_backup,
    verify_backup,
)
from atlas_harness.ops.checklist import (
    RISK_REGISTER,
    CheckResult,
    ReleaseReport,
    RiskControl,
    run_release_checks,
)
from atlas_harness.ops.migrate import (
    ReindexReport,
    SessionReindex,
    rebuild_index,
    rebuild_index_at,
    reindex_session,
)
from atlas_harness.ops.verify import (
    Finding,
    SessionVerification,
    VerifyReport,
    verify_data_dir,
    verify_session,
)

__all__ = [
    "MANIFEST_FILENAME",
    "RISK_REGISTER",
    "BackupManifest",
    "BackupVerification",
    "CheckResult",
    "Finding",
    "ReindexReport",
    "ReleaseReport",
    "RestoreReport",
    "RiskControl",
    "SessionReindex",
    "SessionVerification",
    "VerifyReport",
    "create_backup",
    "read_manifest",
    "rebuild_index",
    "rebuild_index_at",
    "reindex_session",
    "restore_backup",
    "run_release_checks",
    "verify_backup",
    "verify_data_dir",
    "verify_session",
]
