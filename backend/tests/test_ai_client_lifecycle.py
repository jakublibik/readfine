"""The AI client's lifecycle: who closes it, and that somebody always does.

Every provider client owns an httpx connection pool that lives until something
closes it, and one is built per call. Nothing collected them, so the sockets
piled up for as long as the process ran.

These tests cover the two halves of the fix: that a client is released the way
its own SDK wants (Gemini needs two calls, not one), and that the callers cannot
skip it, including the caller who happens to be raising an exception at the time.
"""
import pathlib
import re

import pytest

from app.services import ai_service


class _Recorder:
    """A client that records how it was closed, for each provider's spelling."""

    def __init__(self):
        self.calls = []
        self.aio = _Aio(self)

    async def close(self):  # anthropic, openai, custom
        self.calls.append("close")


class _Aio:
    def __init__(self, parent):
        self._parent = parent

    async def aclose(self):
        self._parent.calls.append("aio.aclose")


class _SyncCloseRecorder(_Recorder):
    """Gemini: the async closer is on .aio, and .close() is the *sync* one that
    releases the second httpx client its constructor builds."""

    def close(self):
        self.calls.append("close")


class TestCloseAiClient:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["anthropic", "openai", "custom"])
    async def test_one_await_closes_the_sdk_client(self, provider):
        client = _Recorder()
        await ai_service.close_ai_client(client, provider)
        assert client.calls == ["close"]

    @pytest.mark.asyncio
    async def test_gemini_needs_both_halves(self):
        """genai.Client builds a sync and an async httpx client in its constructor,
        whether or not either gets used, and each has its own closer. Closing only
        the async one leaves the other holding a pool."""
        client = _SyncCloseRecorder()
        await ai_service.close_ai_client(client, "gemini")
        assert sorted(client.calls) == ["aio.aclose", "close"]

    @pytest.mark.asyncio
    async def test_a_failed_close_does_not_reach_the_caller(self):
        """A socket we did not get back is a smaller problem than a finished answer
        turning into a 500 on the way out of the finally that called this."""

        class Stubborn:
            async def close(self):
                raise RuntimeError("nope")

        await ai_service.close_ai_client(Stubborn(), "anthropic")

    @pytest.mark.asyncio
    async def test_no_client_is_not_an_error(self):
        await ai_service.close_ai_client(None, None)


class TestAiClientContextManager:
    @pytest.mark.asyncio
    async def test_closes_on_the_way_out(self, monkeypatch):
        client = _Recorder()

        async def fake_get(user_id, slot, db):
            return client, "anthropic", "claude-sonnet-5"

        monkeypatch.setattr(ai_service, "get_ai_client", fake_get)
        async with ai_service.ai_client(1, "fast", None) as (c, provider, model):
            assert c is client
            assert (provider, model) == ("anthropic", "claude-sonnet-5")
            assert client.calls == []
        assert client.calls == ["close"]

    @pytest.mark.asyncio
    async def test_closes_when_the_body_raises(self, monkeypatch):
        """The failure path is the one that matters: an AI call that throws is
        ordinary (rate limits, timeouts, a model having a moment), and that is
        exactly when a hand-written close gets skipped."""
        client = _Recorder()

        async def fake_get(user_id, slot, db):
            return client, "anthropic", "claude-sonnet-5"

        monkeypatch.setattr(ai_service, "get_ai_client", fake_get)
        with pytest.raises(ValueError):
            async with ai_service.ai_client(1, "fast", None):
                raise ValueError("provider said no")
        assert client.calls == ["close"]

    @pytest.mark.asyncio
    async def test_an_unconfigured_slot_yields_the_same_triple(self, monkeypatch):
        """Callers check `client is None` before using it, so the context manager
        has to hand that case through rather than raising."""

        async def fake_get(user_id, slot, db):
            return None, None, None

        monkeypatch.setattr(ai_service, "get_ai_client", fake_get)
        async with ai_service.ai_client(1, "fast", None) as triple:
            assert triple == (None, None, None)


class TestSharedTlsContext:
    """Building a TLS context reads and parses the CA bundle, ~9ms of CPU on the
    event loop, and an httpx client builds its own unless handed one. That was
    most of what a client cost to construct, once per article."""

    def test_the_context_is_built_once(self):
        assert ai_service._ssl_context() is ai_service._ssl_context()

    def test_hosted_clients_get_the_shared_context(self, monkeypatch):
        import anthropic  # noqa: F401  imported first: both SDKs subclass
        import openai  # noqa: F401  httpx.AsyncClient while being imported
        import httpx

        seen = []

        class Recording(httpx.AsyncClient):
            def __init__(self, *args, **kwargs):
                seen.append(kwargs.get("verify"))
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", Recording)
        ai_service._make_anthropic_client("sk-ant-test")
        ai_service._make_openai_client("sk-test")
        assert seen == [ai_service._ssl_context(), ai_service._ssl_context()]

    def test_a_custom_endpoint_gets_it_too(self, monkeypatch):
        from app.utils import url_validator

        seen = []
        real = url_validator.PinnedAsyncTransport

        def recording_transport(*args, **kwargs):
            seen.append(kwargs.get("verify"))
            return real(*args, **kwargs)

        monkeypatch.setattr(url_validator, "PinnedAsyncTransport", recording_transport)
        ai_service._make_custom_client(None, "http://localhost:11434/v1")
        assert seen == [ai_service._ssl_context()]

    def test_nothing_outside_ai_service_reaches_for_it(self):
        """The context is shared only among writers that agree on ALPN. httpcore
        calls set_alpn_protocols on whatever context it is given, from each
        connection's own http2 flag, and the feed fetcher runs on HTTP/2
        (url_validator, http2=True) while the AI clients run on HTTP/1.1. One
        context between them would have two writers with different answers."""
        app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
        offenders = [
            path.relative_to(app_dir).as_posix()
            for path in app_dir.rglob("*.py")
            if path.name != "ai_service.py"
            and "_ssl_context" in path.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            "the AI TLS context must not be shared with anything that speaks "
            f"HTTP/2: {offenders}"
        )


class TestNothingElseTakesARawClient:
    """get_ai_client hands out a client nobody closes, and it stays public because
    it is the factory ai_client is built on (and what the tests replace). This is
    the guard that keeps the next caller from reaching for it by name, which is
    how the leak got here in the first place."""

    def test_only_ai_service_calls_get_ai_client(self):
        app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
        pattern = re.compile(r"\bget_ai_client\b")
        offenders = [
            path.relative_to(app_dir).as_posix()
            for path in app_dir.rglob("*.py")
            if path.name != "ai_service.py" and pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            "these call get_ai_client directly and would leak its connection pool; "
            f"use ai_service.ai_client instead: {offenders}"
        )
