"""Time-window transient retry tests — NAC#1312.

Regression suite for the retry-budget redesign: transient network errors
(e.g. a sandbox-proxy restart) are retried within a *time window* measured
from the first failure, instead of a fixed attempt count that a stream of
instant ``connection refused`` failures exhausts within seconds.

Design notes (do NOT weaken):

- These tests drive ``_retry_on_transient`` directly with a deterministic
  fake clock (``monotonic`` reads a counter, ``sleep`` advances it), so
  window exhaustion is exact and the suite never really sleeps.
- ``_reconnect`` is stubbed per-test because these tests target the retry
  *loop* contract; reconnect fidelity itself is covered by
  ``test_e2b_reconnect_1304.py``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from nexau.archs.sandbox import e2b_sandbox as e2b_module
from nexau.archs.sandbox.e2b_sandbox import E2BSandbox

_REFUSED = "connection refused"


class _FakeClock:
    """monotonic() reads a counter; sleep() records and advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    c = _FakeClock()
    monkeypatch.setattr(e2b_module.time, "monotonic", c.monotonic)
    monkeypatch.setattr(e2b_module.time, "sleep", c.sleep)
    return c


def _wrapper(
    monkeypatch: pytest.MonkeyPatch,
    reconnect_errors: list[Exception | None] | None = None,
) -> tuple[E2BSandbox, list[int | None]]:
    """Build a wrapper whose _reconnect is stubbed; returns (wrapper, reconnect_calls)."""
    wrapper = E2BSandbox(sandbox_id="sbx")
    calls: list[int | None] = []
    errors = list(reconnect_errors or [])

    def fake_reconnect(gen_seen: int | None = None) -> None:
        calls.append(gen_seen)
        if errors:
            err = errors.pop(0)
            if err is not None:
                raise err
        wrapper._sandbox_generation += 1

    monkeypatch.setattr(wrapper, "_reconnect", fake_reconnect)
    return wrapper, calls


def _failing_then_ok(failures: int, message: str = _REFUSED) -> tuple[Callable[[], str], list[int]]:
    """fn that raises *failures* transient errors then returns 'ok'."""
    attempts: list[int] = []

    def fn() -> str:
        attempts.append(1)
        if len(attempts) <= failures:
            raise RuntimeError(message)
        return "ok"

    return fn, attempts


class TestWindowBudget:
    def test_recovers_beyond_count_budget_within_window(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """8 连续瞬时失败（> 旧次数制预算 5）在窗口内全部被吸收。"""
        wrapper, reconnects = _wrapper(monkeypatch)
        fn, attempts = _failing_then_ok(failures=8)

        assert wrapper._retry_on_transient(fn) == "ok"
        assert len(attempts) == 9
        assert len(reconnects) == 8
        # 指数退避 0.5→1→2→4 封顶 5s，抖动 ≤ 25%
        expected_bases = [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
        assert len(clock.sleeps) == 8
        for delay, base in zip(clock.sleeps, expected_bases):
            assert base <= delay <= base * 1.25 + 1e-9

    def test_window_exhausted_raises_original_error(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        wrapper, _ = _wrapper(monkeypatch)
        fn, attempts = _failing_then_ok(failures=10_000)

        with pytest.raises(RuntimeError, match=_REFUSED):
            wrapper._retry_on_transient(fn)
        # 退避被 deadline 截断：总耗时恰为一个窗口，不多睡
        assert clock.now == pytest.approx(wrapper.transient_retry_window)
        # 次数远超旧预算，证明窗口内没有隐藏的 count cap
        assert len(attempts) > 6

    def test_window_measured_from_first_failure(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """fn 自身耗时不计入预算：慢调用(50s)失败后仍有完整窗口可重试。"""
        wrapper, _ = _wrapper(monkeypatch)
        attempts: list[int] = []

        def slow_fn() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                clock.now += 50.0  # 模拟 fn 第一次执行本身耗时 50s
                raise RuntimeError(_REFUSED)
            return "ok"

        assert wrapper._retry_on_transient(slow_fn) == "ok"
        assert len(attempts) == 2

    def test_zero_window_falls_back_to_count_mode(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """窗口 <=0 退回旧次数制（self.max_retries）。"""
        wrapper, _ = _wrapper(monkeypatch)
        wrapper.transient_retry_window = 0.0
        fn, attempts = _failing_then_ok(failures=10_000)

        with pytest.raises(RuntimeError, match=_REFUSED):
            wrapper._retry_on_transient(fn)
        assert len(attempts) == wrapper.max_retries + 1

    def test_explicit_max_retries_caps_within_window(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式次数 cap 与窗口取先到者——辅助读取保持低延迟的契约。"""
        wrapper, _ = _wrapper(monkeypatch)
        fn, attempts = _failing_then_ok(failures=10_000)

        with pytest.raises(RuntimeError, match=_REFUSED):
            wrapper._retry_on_transient(fn, max_retries=2)
        assert len(attempts) == 3
        assert clock.now < wrapper.transient_retry_window

    def test_per_call_window_override(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        wrapper, _ = _wrapper(monkeypatch)
        fn, _attempts = _failing_then_ok(failures=10_000)

        with pytest.raises(RuntimeError, match=_REFUSED):
            wrapper._retry_on_transient(fn, retry_window=5.0)
        assert clock.now == pytest.approx(5.0)

    def test_non_transient_error_raises_immediately(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        wrapper, reconnects = _wrapper(monkeypatch)

        def fn() -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            wrapper._retry_on_transient(fn)
        assert not reconnects
        assert not clock.sleeps


class TestReconnectFailureClassification:
    def test_transient_reconnect_failure_does_not_abort(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """断连窗口期控制面同样在抖：reconnect 瞬时失败只记 warning，
        预算继续——旧行为(任何 reconnect 异常立即放弃)会误杀整个窗口。"""
        wrapper, reconnects = _wrapper(
            monkeypatch,
            reconnect_errors=[RuntimeError("service unavailable"), None],
        )
        fn, attempts = _failing_then_ok(failures=2)

        assert wrapper._retry_on_transient(fn) == "ok"
        assert len(attempts) == 3
        assert len(reconnects) == 2

    def test_fatal_reconnect_failure_aborts(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """确定性 reconnect 失败（沙箱已销毁/鉴权失效）立即上抛原始错误，
        不对着已死沙箱空耗窗口。"""
        fatal = ValueError("sandbox was deleted")
        wrapper, reconnects = _wrapper(monkeypatch, reconnect_errors=[fatal])
        fn, attempts = _failing_then_ok(failures=10_000)

        with pytest.raises(RuntimeError, match=_REFUSED) as excinfo:
            wrapper._retry_on_transient(fn)
        assert excinfo.value.__cause__ is fatal
        assert len(attempts) == 1
        assert len(reconnects) == 1


class TestGenerationCapture:
    def test_gen_seen_captured_per_attempt(self, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
        """每次 fn() 前重新采集 generation（NAC#1304 语义在窗口模式下保持）。"""
        wrapper, reconnects = _wrapper(monkeypatch)
        fn, _attempts = _failing_then_ok(failures=3)

        assert wrapper._retry_on_transient(fn) == "ok"
        # 每次 reconnect 收到的 gen_seen 应随代数递增：0,1,2
        assert reconnects == [0, 1, 2]
