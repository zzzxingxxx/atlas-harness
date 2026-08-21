"""Hard process exits around each event must leave a rebuildable log.

The child process is killed with ``os._exit`` so neither the SQLite connection
nor the Python interpreter gets a chance to clean up.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from atlas_harness.events import EventStore

SESSION_ID = "ses_restart"
StoreFactory = Callable[..., EventStore]

CHILD = """
import os
import sys
from pathlib import Path

from atlas_harness.events import EventStore, EventType

data_dir, session_id, appends = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
store = EventStore(data_dir)
if store.next_seq(session_id) == 1:
    store.append_new(EventType.SESSION_CREATED, session_id=session_id, payload={"title": "t"})
for _ in range(appends):
    seq = store.next_seq(session_id)
    store.append_new(
        EventType.ASSISTANT_MESSAGE, session_id=session_id, payload={"content": f"m{seq}"}
    )
sys.stdout.write(str(store.next_seq(session_id) - 1))
sys.stdout.flush()
os._exit(0)
"""


def run_child(data_dir: Path, appends: int) -> int:
    result = subprocess.run(
        [sys.executable, "-c", CHILD, str(data_dir), SESSION_ID, str(appends)],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout)


@pytest.mark.parametrize("appends", [0, 1, 2, 3])
def test_state_is_rebuilt_after_a_hard_exit(
    store_factory: StoreFactory, tmp_path: Path, appends: int
) -> None:
    data_dir = tmp_path / "runtime"

    last_seq = run_child(data_dir, appends)
    store = store_factory(data_dir)

    state = store.load_state(SESSION_ID)

    assert state.last_seq == last_seq == appends + 1
    assert state.event_count == appends + 1
    assert state.messages == [f"m{seq}" for seq in range(2, appends + 2)]
    summary = store.index.get_session(SESSION_ID)
    assert summary is not None
    assert summary.last_seq == last_seq


def test_repeated_restarts_keep_appending(store_factory: StoreFactory, tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"

    for expected in (2, 3, 4):
        assert run_child(data_dir, 1) == expected

    store = store_factory(data_dir)
    state = store.load_state(SESSION_ID)

    assert state.last_seq == 4
    assert state.messages == ["m2", "m3", "m4"]
    assert [event.seq for event in store.read_events(SESSION_ID)] == [1, 2, 3, 4]
