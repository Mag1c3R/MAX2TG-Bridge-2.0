"""Tests for app/max_version.py — web client version tracking."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app import max_version
from app.max_version import describe_change, parse_bundle


BUNDLE = 'var cd=e({}),ld=`wss://api.oneme.ru/websocket`,ud=`26.11.9`,dd=`22367`'


class TestParseBundle:
    def test_finds_url_and_version(self):
        assert parse_bundle(BUNDLE) == ("wss://api.oneme.ru/websocket", "26.11.9")

    def test_other_quotes(self):
        assert parse_bundle('a="wss://x.oneme.ru/websocket", b="27.1.0"') == ("wss://x.oneme.ru/websocket", "27.1.0")

    def test_missing(self):
        assert parse_bundle("nothing here") is None


class TestDescribeChange:
    def test_same(self):
        assert describe_change("wss://a/websocket", "1.0.0", "wss://a/websocket", "1.0.0") is None

    def test_version_bump(self):
        text = describe_change("wss://a/websocket", "1.0.0", "wss://a/websocket", "1.1.0")
        assert "1.0.0" in text and "1.1.0" in text and "обновил" in text

    def test_url_change_is_loud(self):
        text = describe_change("wss://a/websocket", "1.0.0", "wss://b/websocket", "1.0.0")
        assert text.startswith("🚨") and "wss://b/websocket" in text


class TestWatch:
    async def test_updates_client_and_notifies_once(self, monkeypatch):
        client = MagicMock(WS_URL="wss://api.oneme.ru/websocket", app_version="26.11.9")
        monkeypatch.setattr(max_version, "detect", AsyncMock(return_value=("wss://api.oneme.ru/websocket", "26.12.0")))
        notify = AsyncMock()
        task = asyncio.create_task(max_version.watch(client, notify, every=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        assert client.app_version == "26.12.0"
        notify.assert_awaited_once()

    async def test_failed_check_changes_nothing(self, monkeypatch):
        client = MagicMock(WS_URL="wss://api.oneme.ru/websocket", app_version="26.11.9")
        monkeypatch.setattr(max_version, "detect", AsyncMock(return_value=None))
        notify = AsyncMock()
        task = asyncio.create_task(max_version.watch(client, notify, every=0.01))
        await asyncio.sleep(0.03)
        task.cancel()
        assert client.app_version == "26.11.9"
        notify.assert_not_awaited()


class TestDetectHeaders:
    async def test_brotli_is_never_requested(self):
        seen = []

        class Resp:
            status = 200
            def __init__(self, body): self.body = body
            async def text(self): return self.body
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class Session:
            def get(self, url, headers=None, timeout=None):
                seen.append(headers["Accept-Encoding"])
                return Resp('<script src="/a.js"></script>' if url.endswith("/") else BUNDLE)

        found = await max_version.detect(Session(), {"Accept-Encoding": "gzip, deflate, br, zstd"})
        assert found == ("wss://api.oneme.ru/websocket", "26.11.9")
        assert seen == ["gzip, deflate", "gzip, deflate"]
