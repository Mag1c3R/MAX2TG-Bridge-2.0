"""Tests for app/tg_handler.py — topic-based reply routing."""

from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    TOPIC_STORE_KEY,
    _on_topic_message,
    build_tg_app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_topic_store(mapping: dict | None = None):
    """A TopicStore stand-in: chat_for_topic(thread_id) → max_chat_id."""
    mapping = mapping or {10: 42}
    store = MagicMock()
    store.chat_for_topic = MagicMock(side_effect=lambda tid: mapping.get(tid))
    return store


def _make_update(text="Hello", thread_id=10, is_topic_message=True, user_id=100):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.is_topic_message = is_topic_message
    update.message.reply_text = AsyncMock()
    update.message.set_reaction = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_context(max_client=None, topic_store=None, allowed_user_id=None):
    ctx = MagicMock()
    bot_data = {ALLOWED_USER_KEY: allowed_user_id}
    if max_client is not None:
        bot_data[MAX_CLIENT_KEY] = max_client
    if topic_store is not None:
        bot_data[TOPIC_STORE_KEY] = topic_store
    ctx.bot_data = bot_data
    return ctx


# ---------------------------------------------------------------------------
# _on_topic_message
# ---------------------------------------------------------------------------

class TestOnTopicMessage:
    async def test_routes_topic_text_to_max(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update("Hello", thread_id=10)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once_with(42, "Hello", elements=[], reply_to=None)

    async def test_success_has_no_reaction(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.set_reaction.assert_not_called()

    async def test_failed_send_gets_thumbs_down(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"_max_error": {"message": "failed"}})
        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.set_reaction.assert_awaited_once_with("👎")
        update.message.reply_text.assert_awaited_once()

    async def test_send_exception_gets_thumbs_down(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("failed"))
        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.set_reaction.assert_awaited_once_with("👎")

    async def test_ignores_general_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=None, is_topic_message=False)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_unknown_topic(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(thread_id=999)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_ignores_empty_text(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(text=None)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_respects_allowed_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock()

        update = _make_update(user_id=555)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_not_called()

    async def test_allows_matching_user_id(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value={"ok": True})

        update = _make_update(user_id=100)
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store(),
                            allowed_user_id=100)

        await _on_topic_message(update, ctx)

        max_client.send_message.assert_called_once()

    async def test_warns_when_max_client_missing(self):
        update = _make_update()
        ctx = _make_context(topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_send_failure(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=None)

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]

    async def test_warns_on_exception(self):
        max_client = MagicMock()
        max_client.send_message = AsyncMock(side_effect=RuntimeError("boom"))

        update = _make_update()
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store())

        await _on_topic_message(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "⚠️" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# build_tg_app
# ---------------------------------------------------------------------------

class TestBuildTgApp:
    def test_wires_bot_data(self):
        max_client = MagicMock()
        topic_store = _make_topic_store()

        app = build_tg_app("123456:AAABBBCCC", max_client, "-100123456",
                            topic_store, allowed_user_id=777)

        assert app.bot_data[MAX_CLIENT_KEY] is max_client
        assert app.bot_data[TOPIC_STORE_KEY] is topic_store
        assert app.bot_data[ALLOWED_USER_KEY] == 777

    def test_allowed_user_id_none_when_unset(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.bot_data[ALLOWED_USER_KEY] is None

    def test_registers_message_handler(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456",
                            _make_topic_store())

        assert app.handlers[0]


# ---------------------------------------------------------------------------
# Telegram reply → MAX reply (quote)
# ---------------------------------------------------------------------------

from app.recovery import RecoveryStore
from app.tg_handler import LINK_STORE_KEY


def _reply_update(reply_to_id, thread_id=10, message_id=500):
    update = _make_update("ответ", thread_id=thread_id)
    update.message.message_id = message_id
    update.message.entities = []
    reply = MagicMock()
    reply.message_id = reply_to_id
    reply.forum_topic_created = None
    update.message.reply_to_message = reply
    return update


class TestReplyQuote:
    def _ctx(self, tmp_path, resp=None):
        store = RecoveryStore(str(tmp_path / "r.sqlite3"))
        max_client = MagicMock()
        max_client.send_message = AsyncMock(return_value=resp or {"message": {"id": 999}})
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))
        ctx.bot_data[LINK_STORE_KEY] = store
        return ctx, store, max_client

    async def test_reply_quotes_linked_max_message(self, tmp_path):
        ctx, store, max_client = self._ctx(tmp_path)
        store.link_tg(300, 42, "115000000000000001")
        await _on_topic_message(_reply_update(300), ctx)
        assert max_client.send_message.await_args.kwargs["reply_to"] == "115000000000000001"

    async def test_topic_root_is_not_a_reply(self, tmp_path):
        ctx, store, max_client = self._ctx(tmp_path)
        store.link_tg(10, 42, "1")
        await _on_topic_message(_reply_update(10, thread_id=10), ctx)
        assert max_client.send_message.await_args.kwargs["reply_to"] is None

    async def test_link_from_other_chat_is_ignored(self, tmp_path):
        ctx, store, max_client = self._ctx(tmp_path)
        store.link_tg(300, 77, "1")
        await _on_topic_message(_reply_update(300), ctx)
        assert max_client.send_message.await_args.kwargs["reply_to"] is None

    async def test_own_sent_message_is_linked(self, tmp_path):
        ctx, store, _ = self._ctx(tmp_path, resp={"message": {"id": 999}})
        await _on_topic_message(_reply_update(300, message_id=501), ctx)
        assert store.max_for_tg(501) == ("42", "999")


# ---------------------------------------------------------------------------
# Telegram edit → MAX edit
# ---------------------------------------------------------------------------

from app.tg_handler import _on_topic_edit


def _edit_update(text="исправлено", thread_id=10, message_id=501):
    update = MagicMock()
    update.message = None
    msg = MagicMock()
    msg.text = text
    msg.entities = []
    msg.message_id = message_id
    msg.message_thread_id = thread_id
    msg.is_topic_message = True
    msg.reply_text = AsyncMock()
    msg.set_reaction = AsyncMock()
    update.edited_message = msg
    update.effective_user = MagicMock()
    update.effective_user.id = 100
    return update


class TestEditMirror:
    def _ctx(self, tmp_path, resp=None):
        store = RecoveryStore(str(tmp_path / "r.sqlite3"))
        max_client = MagicMock()
        max_client.edit_message = AsyncMock(return_value=resp or {"message": {"id": 999}})
        ctx = _make_context(max_client=max_client, topic_store=_make_topic_store({10: 42}))
        ctx.bot_data[LINK_STORE_KEY] = store
        return ctx, store, max_client

    async def test_edit_of_own_message_is_mirrored(self, tmp_path):
        ctx, store, max_client = self._ctx(tmp_path)
        store.link_tg(501, 42, "999")
        await _on_topic_edit(_edit_update("исправлено"), ctx)
        max_client.edit_message.assert_awaited_once_with(42, "999", "исправлено", elements=[])

    async def test_unknown_message_is_ignored(self, tmp_path):
        ctx, _, max_client = self._ctx(tmp_path)
        await _on_topic_edit(_edit_update(), ctx)
        max_client.edit_message.assert_not_awaited()

    async def test_message_from_other_chat_is_ignored(self, tmp_path):
        ctx, store, max_client = self._ctx(tmp_path)
        store.link_tg(501, 77, "999")
        await _on_topic_edit(_edit_update(), ctx)
        max_client.edit_message.assert_not_awaited()

    async def test_failed_edit_gets_thumbs_down(self, tmp_path):
        ctx, store, _ = self._ctx(tmp_path, resp={"_max_error": {"message": "nope"}})
        store.link_tg(501, 42, "999")
        update = _edit_update()
        await _on_topic_edit(update, ctx)
        update.edited_message.set_reaction.assert_awaited_once_with("👎")

    def test_edit_handler_registered_in_own_group(self):
        app = build_tg_app("123456:AAABBBCCC", MagicMock(), "-100123456", _make_topic_store())
        assert any(h.callback is _on_topic_edit for h in app.handlers.get(1, []))
        assert all(h.callback is not _on_topic_edit for h in app.handlers[0])


from datetime import timedelta
from app.tg_handler import _duration_ms


class TestDurationMs:
    def test_int_seconds(self):
        assert _duration_ms(3) == 3000

    def test_timedelta(self):
        assert _duration_ms(timedelta(seconds=2.5)) == 2500

    def test_none(self):
        assert _duration_ms(None) == 0
