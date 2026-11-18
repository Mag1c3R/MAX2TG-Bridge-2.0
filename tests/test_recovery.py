import asyncio
import contextvars
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.max_client import MaxClient
from app.max_listener import create_max_client
from app.recovery import RecoveryStore


def test_recovery_store_survives_restart(tmp_path):
    path = str(tmp_path / 'recovery.sqlite3')
    one = RecoveryStore(path)
    one.set('pending_since', 123)
    one.mark(10, 20)
    two = RecoveryStore(path)
    assert two.get('pending_since') == 123
    assert two.seen(10, 20)
    assert not two.seen(10, 21)


@pytest.mark.asyncio
async def test_reconnect_fetches_missed_message_once(tmp_path):
    store = RecoveryStore(str(tmp_path / 'recovery.sqlite3'))
    sender = MagicMock()
    sender.send = AsyncMock()
    sender.ensure_topic = AsyncMock(return_value=123)
    sender.topic_store._chats = {'10': {'topic_id': 123, 'title': 'Peer'}}
    sender.topic_store.get_topic.return_value = 123
    sender.require_success = contextvars.ContextVar('strict', default=False)
    client = create_max_client('token', 'device', sender, recovery=store)
    client.resolver.users[1] = 'Self'
    client.resolver.users[2] = 'Peer'
    snapshot = {'profile': {'id': 1, 'names': []}, 'chats': [{'id': 10, 'type': 'DIALOG', 'participants': {'1': {}, '2': {}}}]}
    await client._on_ready_cb(snapshot)
    await asyncio.sleep(0)
    now = int(time.time() * 1000)
    old = {'id': '101', 'time': now - 5000, 'sender': 2, 'text': 'old', 'attaches': []}
    new = {'id': '102', 'time': now - 1000, 'sender': 2, 'text': 'missed', 'attaches': []}
    await client._on_message_cb(client.parse_history_message(10, old))
    await client._on_disconnect_cb()
    client.fetch_history = AsyncMock(side_effect=[[new, old], []])
    await client._on_ready_cb(snapshot)
    await asyncio.sleep(0.05)
    bodies = [call.args[0] for call in sender.send.call_args_list]
    assert sum('old' in body for body in bodies) == 1
    assert sum('missed' in body for body in bodies) == 1
    assert store.get('pending_since') == 0
    assert client.fetch_history.await_count == 2
    await client._on_disconnect_cb()


def test_tg_links_roundtrip(tmp_path):
    store = RecoveryStore(str(tmp_path / 'recovery.sqlite3'))
    assert store.max_for_tg(1) is None
    store.link_tg(1, -70, 123456789012345678)
    assert RecoveryStore(str(tmp_path / 'recovery.sqlite3')).max_for_tg(1) == ('-70', '123456789012345678')
