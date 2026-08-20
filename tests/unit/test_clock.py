import pytest

from atlas_harness.kernel.clock import FrozenClock


def test_frozen_clock_is_deterministic() -> None:
    clock = FrozenClock(100)

    clock.advance(25)

    assert clock.now_ms() == 125


def test_frozen_clock_rejects_backwards_time() -> None:
    clock = FrozenClock()

    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(-1)
