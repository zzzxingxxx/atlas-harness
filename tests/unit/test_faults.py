import pytest

from atlas_harness.kernel import FaultInjected, FaultInjector


def test_check_is_a_noop_without_armed_points() -> None:
    injector = FaultInjector()

    injector.check("anything")

    assert injector.triggered("anything") == 0


def test_armed_point_fires_once() -> None:
    injector = FaultInjector()
    injector.arm("store.write")

    with pytest.raises(FaultInjected):
        injector.check("store.write")
    injector.check("store.write")

    assert injector.triggered("store.write") == 1
    assert not injector.is_armed("store.write")


def test_other_points_are_unaffected() -> None:
    injector = FaultInjector()
    injector.arm("a")

    injector.check("b")

    assert injector.is_armed("a")


def test_arm_with_repeat_count() -> None:
    injector = FaultInjector()
    injector.arm("p", times=2)

    for _ in range(2):
        with pytest.raises(FaultInjected):
            injector.check("p")
    injector.check("p")

    assert injector.triggered("p") == 2


def test_arm_with_custom_error() -> None:
    injector = FaultInjector()
    injector.arm("p", error=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        injector.check("p")


def test_arm_rejects_non_positive_times() -> None:
    with pytest.raises(ValueError, match="times must be >= 1"):
        FaultInjector().arm("p", times=0)


def test_disarm_and_clear() -> None:
    injector = FaultInjector()
    injector.arm("a")
    injector.arm("b")

    injector.disarm("a")
    injector.disarm("missing")
    assert not injector.is_armed("a")
    assert injector.is_armed("b")

    injector.clear()
    assert not injector.is_armed("b")
    injector.check("b")
