import pytest

from atlas_harness.kernel.errors import LifecycleError
from atlas_harness.kernel.lifecycle import Lifecycle, LifecycleState


@pytest.mark.asyncio
async def test_lifecycle_start_cancel_close() -> None:
    lifecycle = Lifecycle()

    await lifecycle.start()
    await lifecycle.request_cancel()

    assert lifecycle.state is LifecycleState.RUNNING
    assert lifecycle.cancelled

    await lifecycle.close()

    assert lifecycle.state is LifecycleState.CLOSED


@pytest.mark.asyncio
async def test_lifecycle_rejects_close_before_start() -> None:
    with pytest.raises(LifecycleError):
        await Lifecycle().close()
