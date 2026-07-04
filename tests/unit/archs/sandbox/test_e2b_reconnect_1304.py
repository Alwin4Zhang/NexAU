"""Reconnect fidelity tests for self-host (force_http) deployments — NAC#1304.

Regression suite for the P0 where a single transient error made
``E2BSandbox._reconnect()`` install an SDK-default HTTPS object
(``https://49983-{id}.{domain}:443``) on deployments whose sandbox service
only listens on HTTP :80, permanently breaking every subsequent sandbox
operation in the run.

Design notes (do NOT weaken):

- These tests intentionally use the REAL ``e2b`` SDK ``ConnectionConfig`` /
  ``Sandbox.__init__`` (local construction, no network) and only mock the
  single network call ``SandboxApi._cls_connect``. Testing through the
  ``_FakeSandboxClass`` fixture would turn the assertions into constructor
  tautologies and miss the actual bug mechanism (the SDK default URL scheme).
- ``test_sdk_default_connect_builds_https`` characterizes the SDK's default
  behavior that makes the bug possible; if an SDK upgrade changes it, this
  suite must be re-evaluated rather than silently trusted.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from packaging.version import Version

e2b = pytest.importorskip("e2b")

from e2b.api.client_sync import TransportWithLogger, get_transport  # noqa: E402
from e2b.connection_config import ConnectionConfig  # noqa: E402
from e2b.sandbox_sync.sandbox_api import SandboxApi  # noqa: E402

from nexau.archs.sandbox import e2b_sandbox as e2b_module  # noqa: E402
from nexau.archs.sandbox.base_sandbox import (  # noqa: E402
    SandboxError,
    extract_dataclass_init_kwargs,
)
from nexau.archs.sandbox.e2b_sandbox import (  # noqa: E402
    E2BSandbox,
    _locked_build_http_sandbox,
)

SID = "00000000-0000-0000-0000-00000000abcd"
DOMAIN = "sandbox.test.svc.cluster.local"


@pytest.fixture(autouse=True)
def _pinned_environment(monkeypatch: pytest.MonkeyPatch):
    """Pin env vars that alter SDK URL construction, and isolate the singleton.

    E2B_DEBUG / E2B_SANDBOX_URL / E2B_DOMAIN silently change
    ``ConnectionConfig.get_sandbox_url`` — a developer machine exporting any
    of them would make the characterization tests flap.
    """
    for var in ("E2B_DEBUG", "E2B_SANDBOX_URL", "E2B_DOMAIN", "E2B_FORCE_HTTP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    prev = TransportWithLogger.singleton
    TransportWithLogger.singleton = None
    yield
    TransportWithLogger.singleton = prev


def _connect_response(token: str | None = "tok-1") -> SimpleNamespace:
    """Shape consumed by the SDK's ``_cls_connect_sandbox``."""
    return SimpleNamespace(
        sandbox_id=SID,
        domain=DOMAIN,
        envd_version="0.2.0",
        envd_access_token=token,
        traffic_access_token=None,
    )


def _mock_connect(monkeypatch: pytest.MonkeyPatch, responses: list[SimpleNamespace]) -> list[dict]:
    """Mock the only network call; returns the recorded call list."""
    calls: list[dict] = []

    def fake_cls_connect(cls: type, *args: object, **kwargs: object) -> SimpleNamespace:
        # 兼容 SDK 不同版本的位置/关键字调用形态
        calls.append({"args": args, **kwargs})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(SandboxApi, "_cls_connect", classmethod(fake_cls_connect))
    return calls


def _wrapper(force_http: bool) -> E2BSandbox:
    wrapper = E2BSandbox(sandbox_id=SID, _force_http=force_http)
    wrapper.set_api_credentials("test-key", "http://sandbox-manager:8008")
    return wrapper


class TestSdkCharacterization:
    def test_sdk_default_connect_builds_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the SDK behavior the bug depends on: bare connect() => https."""
        _mock_connect(monkeypatch, [_connect_response()])
        assert e2b_module.Sandbox is not None
        raw = e2b_module.Sandbox.connect(sandbox_id=SID, api_key="test-key", api_url="http://api")
        assert raw.envd_api_url.startswith("https://"), (
            "SDK default connect() no longer builds an https envd URL — "
            "re-evaluate the NAC#1304 force_http rebuild before trusting this suite"
        )

    def test_sdk_private_interface_contract(self) -> None:
        """Pin the private SDK surface the fix depends on (fails CI on rename)."""
        from e2b.api import limits

        cc = ConnectionConfig()
        assert hasattr(cc, "_sandbox_url")
        assert hasattr(cc, "proxy")
        assert hasattr(TransportWithLogger, "singleton")
        # pre-seed constructs its own transport with this exact signature
        fresh = TransportWithLogger(limits=limits, proxy=None)
        assert fresh.pool is not None
        sb = _locked_build_http_sandbox(SID, DOMAIN, "tok", Version("0.2.0"))
        assert hasattr(sb, "_envd_access_token")
        assert hasattr(sb, "_envd_version")
        assert hasattr(sb, "_transport")


class TestLockedBuild:
    def test_locked_build_preseeds_and_detaches(self) -> None:
        sb = _locked_build_http_sandbox(SID, DOMAIN, "tok", Version("0.2.0"))
        # detach: the new object's transport is privatized
        assert TransportWithLogger.singleton is None
        assert sb._transport is not None
        assert sb.envd_api_url == f"http://49983-{SID}.{DOMAIN}"
        # a later caller gets a NEW transport — never feeds connections into
        # the privatized pool of `sb`
        other = get_transport(sb.connection_config)
        assert other is not sb._transport
        assert sb.connection_config.sandbox_headers.get("X-Access-Token") == "tok"


class TestReconnectFidelity:
    def test_reconnect_rebuilds_http_when_force_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_connect(monkeypatch, [_connect_response()])
        wrapper = _wrapper(force_http=True)
        wrapper._reconnect()
        assert wrapper._sandbox is not None
        assert wrapper._sandbox.envd_api_url == f"http://49983-{SID}.{DOMAIN}"
        assert wrapper._sandbox.connection_config.sandbox_headers.get("X-Access-Token") == "tok-1"
        assert wrapper._sandbox_generation == 1

    def test_reconnect_keeps_default_when_not_force_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SaaS / official-E2B regression: no rebuild when force_http is off."""
        _mock_connect(monkeypatch, [_connect_response()])
        wrapper = _wrapper(force_http=False)
        wrapper._reconnect()
        assert wrapper._sandbox is not None
        assert wrapper._sandbox.envd_api_url.startswith("https://")
        assert wrapper._sandbox_generation == 1

    def test_reconnect_fail_closed_keeps_old_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing token in response AND in fallback => raise, keep old object.

        Then a later reconnect with a token-bearing response must still rebuild
        (the v1 design's self-destructing criterion regression).
        """
        _mock_connect(monkeypatch, [_connect_response(token=None), _connect_response(token="tok-2")])
        wrapper = _wrapper(force_http=True)
        old = SimpleNamespace(sandbox_id=SID, sandbox_domain=DOMAIN, _envd_access_token=None)
        wrapper.__dict__["_sandbox"] = old

        with pytest.raises(SandboxError, match="force_http rebuild requires"):
            wrapper._reconnect()
        assert wrapper._sandbox is old  # fail-closed: old object kept
        assert wrapper._sandbox_generation == 0

        wrapper._reconnect()  # second attempt with a good response succeeds
        assert wrapper._sandbox is not None
        assert wrapper._sandbox is not old
        assert wrapper._sandbox.envd_api_url == f"http://49983-{SID}.{DOMAIN}"
        assert wrapper._sandbox_generation == 1

    def test_reconnect_token_fallback_uses_previous_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_connect(monkeypatch, [_connect_response(token=None)])
        wrapper = _wrapper(force_http=True)
        old = SimpleNamespace(sandbox_id=SID, sandbox_domain=DOMAIN, _envd_access_token="old-tok")
        wrapper.__dict__["_sandbox"] = old

        wrapper._reconnect()
        assert wrapper._sandbox is not None
        assert wrapper._sandbox is not old
        assert wrapper._sandbox.connection_config.sandbox_headers.get("X-Access-Token") == "old-tok"


class TestReconnectConcurrency:
    def test_concurrent_reconnect_single_flight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A thundering herd sharing one gen_seen performs exactly one reconnect."""
        n_threads = 4
        barrier = threading.Barrier(n_threads)
        calls = _mock_connect(monkeypatch, [_connect_response()])
        # widen the race window so an unlocked implementation would overlap
        original = SandboxApi._cls_connect.__func__

        def slow_connect(cls: type, *args: object, **kwargs: object) -> SimpleNamespace:
            result = original(cls, *args, **kwargs)
            time.sleep(0.05)
            return result

        monkeypatch.setattr(SandboxApi, "_cls_connect", classmethod(slow_connect))

        wrapper = _wrapper(force_http=True)
        gen_seen = wrapper._sandbox_generation
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                wrapper._reconnect(gen_seen)
            except BaseException as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(calls) == 1, f"expected single-flight, got {len(calls)} connects"
        assert wrapper._sandbox_generation == gen_seen + 1

    def test_sequential_transients_reconnect_each_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gen_seen must be captured per attempt: two sequential transients on
        the same wrapper trigger two reconnects (a 'capture once before the
        loop' implementation would silently skip the second one)."""
        calls = _mock_connect(monkeypatch, [_connect_response()])
        monkeypatch.setattr(e2b_module.time, "sleep", lambda _s: None)  # skip backoff
        wrapper = _wrapper(force_http=True)

        attempts: list[int] = []

        def fn() -> str:
            attempts.append(1)
            if len(attempts) <= 2:
                raise RuntimeError("connection refused")  # matches _TRANSIENT_PATTERNS
            return "ok"

        assert wrapper._retry_on_transient(fn) == "ok"
        assert len(calls) == 2, "each failed attempt must reconnect against its own generation"
        assert wrapper._sandbox_generation == 2


class TestForceHttpPersistence:
    def test_force_http_round_trips_through_dict(self) -> None:
        wrapper = E2BSandbox(sandbox_id=SID, _force_http=True)
        state = wrapper.dict()
        assert state.get("_force_http") is True
        # concurrency primitives must never leak into persisted state
        assert "_reconnect_lock" not in state
        assert "_sandbox_generation" not in state

        restored = E2BSandbox(**extract_dataclass_init_kwargs(E2BSandbox, state))
        assert restored._force_http is True

    def test_force_http_defaults_false(self) -> None:
        assert E2BSandbox(sandbox_id=SID)._force_http is False


class TestStaticReconnect:
    """NAC#1312 CR: explicit-target 直绑路径的原位静态重建。

    连接参数是构造时显式给定的静态事实——reconnect 绝不回访控制面
    ``Sandbox.connect()``（会引入 SDK 默认域兜底与带外沙箱 NotFound
    两类回归），直接用当前对象自带的 domain/token 重建。
    """

    def test_static_reconnect_never_calls_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _mock_connect(monkeypatch, [_connect_response()])
        wrapper = E2BSandbox(sandbox_id=SID, _force_http=True, _static_reconnect=True)
        wrapper.set_api_credentials("test-key", "http://sandbox-manager:8008")
        old = _locked_build_http_sandbox(SID, DOMAIN, "static-tok", Version("0.2.0"))
        wrapper.__dict__["_sandbox"] = old

        wrapper._reconnect()

        assert not calls, "static reconnect 不得触碰控制面 Sandbox.connect()"
        assert wrapper._sandbox is not None
        assert wrapper._sandbox is not old
        assert wrapper._sandbox.envd_api_url == f"http://49983-{SID}.{DOMAIN}"
        assert wrapper._sandbox.connection_config.sandbox_headers.get("X-Access-Token") == "static-tok"
        assert wrapper._sandbox_generation == 1

    def test_static_reconnect_fail_closed_without_object(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无现存对象时 fail-closed（绝不退回 connect 路径静默兜底）。"""
        calls = _mock_connect(monkeypatch, [_connect_response()])
        wrapper = E2BSandbox(sandbox_id=SID, _force_http=True, _static_reconnect=True)
        wrapper.set_api_credentials("test-key", "http://sandbox-manager:8008")

        with pytest.raises(SandboxError, match="Static reconnect requires an existing sandbox"):
            wrapper._reconnect()
        assert not calls
        assert wrapper._sandbox_generation == 0

    def test_static_reconnect_fail_closed_missing_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _mock_connect(monkeypatch, [_connect_response()])
        wrapper = E2BSandbox(sandbox_id=SID, _force_http=True, _static_reconnect=True)
        wrapper.set_api_credentials("test-key", "http://sandbox-manager:8008")
        old = SimpleNamespace(sandbox_id=SID, sandbox_domain=DOMAIN, _envd_access_token=None, _envd_version=Version("0.2.0"))
        wrapper.__dict__["_sandbox"] = old

        with pytest.raises(SandboxError, match="Static reconnect requires sandbox domain and envd access token"):
            wrapper._reconnect()
        assert not calls
        assert wrapper._sandbox is old

    def test_static_reconnect_round_trips_through_dict(self) -> None:
        wrapper = E2BSandbox(sandbox_id=SID, _static_reconnect=True)
        state = wrapper.dict()
        assert state.get("_static_reconnect") is True
        restored = E2BSandbox(**extract_dataclass_init_kwargs(E2BSandbox, state))
        assert restored._static_reconnect is True
