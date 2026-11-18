"""Tests for app/max_client.py — OpCode enum and _parse_message."""

import pytest
from unittest.mock import AsyncMock
from app.max_client import MaxClient, MaxMessage, OpCode


# ---------------------------------------------------------------------------
# OpCode enum
# ---------------------------------------------------------------------------

class TestOpCode:
    """Validate that all expected opcodes exist with their correct integer values."""

    def test_heartbeat_ping(self):
        assert OpCode.HEARTBEAT_PING == 1

    def test_handshake(self):
        assert OpCode.HANDSHAKE == 6

    def test_auth_snapshot(self):
        assert OpCode.AUTH_SNAPSHOT == 19

    def test_logout(self):
        assert OpCode.LOGOUT == 20

    def test_sticker_store(self):
        assert OpCode.STICKER_STORE == 27

    def test_asset_get(self):
        assert OpCode.ASSET_GET == 28

    def test_favorite_sticker(self):
        assert OpCode.FAVORITE_STICKER == 29

    def test_contact_get(self):
        assert OpCode.CONTACT_GET == 32

    def test_contact_presence(self):
        assert OpCode.CONTACT_PRESENCE == 35

    def test_chat_get(self):
        assert OpCode.CHAT_GET == 48

    def test_send_message(self):
        assert OpCode.SEND_MESSAGE == 64

    def test_media_play_opcodes(self):
        assert OpCode.VIDEO_PLAY == 83
        assert OpCode.AUDIO_PLAY == 301

    def test_edit_message(self):
        assert OpCode.EDIT_MESSAGE == 67

    def test_dispatch(self):
        assert OpCode.DISPATCH == 128

    def test_all_values_are_ints(self):
        for member in OpCode:
            assert isinstance(member.value, int), f"{member.name} is not an int"

    def test_no_duplicate_values(self):
        values = [m.value for m in OpCode]
        assert len(values) == len(set(values)), "Duplicate opcode values found"

    def test_can_be_used_as_int(self):
        # IntEnum should compare equal to a plain int
        assert OpCode.HANDSHAKE == 6
        assert 6 == OpCode.HANDSHAKE


class TestMediaPlaybackUrls:
    async def test_audio_play_returns_opus_url(self):
        client = MaxClient(token="tok", device_id="dev")
        url = "https://a.oneme.ru/audio?signature=private"
        client.cmd = AsyncMock(return_value={"opus": url})

        result = await client.download_audio_url(12, 34, "56", token="secret")

        assert result == url
        client.cmd.assert_awaited_once_with(
            OpCode.AUDIO_PLAY,
            {"audioId": 12, "chatId": 34, "messageId": 56, "token": "secret"},
        )

    async def test_video_play_prefers_mp4(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value={
            "EXTERNAL": "https://m.ok.ru/video/1",
            "MP4_240": "https://v.oneme.ru/low",
            "MP4_480": "https://v.oneme.ru/high",
        })

        result = await client.download_video_url(12, 34, "56")

        assert result == "https://v.oneme.ru/high"
        client.cmd.assert_awaited_once_with(
            OpCode.VIDEO_PLAY,
            {"videoId": 12, "chatId": 34, "messageId": 56},
        )

    async def test_rejects_untrusted_media_url(self):
        client = MaxClient(token="tok", device_id="dev")
        client.cmd = AsyncMock(return_value={"opus": "https://elsewhere.example/audio"})
        assert await client.download_audio_url(12, 34, "56") is None


# ---------------------------------------------------------------------------
# MaxMessage dataclass defaults
# ---------------------------------------------------------------------------

class TestMaxMessageDefaults:
    def test_default_text_is_empty_string(self):
        msg = MaxMessage()
        assert msg.text == ""

    def test_default_is_self_is_false(self):
        msg = MaxMessage()
        assert msg.is_self is False

    def test_default_attaches_is_empty_list(self):
        msg = MaxMessage()
        assert msg.attaches == []

    def test_default_link_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.link == {}

    def test_default_raw_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.raw == {}

    def test_attaches_are_independent_instances(self):
        # mutable default via field(default_factory=...) must not be shared
        m1 = MaxMessage()
        m2 = MaxMessage()
        m1.attaches.append("x")
        assert m2.attaches == []


# ---------------------------------------------------------------------------
# MaxClient._parse_message
# ---------------------------------------------------------------------------

def _make_client() -> MaxClient:
    return MaxClient(token="tok", device_id="dev")


class TestParseMessage:
    """Tests for _parse_message — the only complex pure-ish method."""

    def test_returns_none_when_no_message_key(self):
        client = _make_client()
        assert client._parse_message({}) is None

    def test_returns_none_when_message_is_not_dict(self):
        client = _make_client()
        assert client._parse_message({"message": "oops"}) is None
        assert client._parse_message({"message": 42}) is None
        assert client._parse_message({"message": None}) is None

    def test_basic_text_message(self):
        client = _make_client()
        payload = {
            "chatId": 100,
            "message": {
                "sender": 7,
                "text": "Hello",
                "time": 1700000000000,
                "id": "abc123",
            },
        }
        msg = client._parse_message(payload)
        assert msg is not None
        assert msg.chat_id == 100
        assert msg.sender_id == 7
        assert msg.text == "Hello"
        assert msg.timestamp == 1700000000000
        assert msg.message_id == "abc123"

    def test_message_id_is_always_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"id": 99999}}
        msg = client._parse_message(payload)
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "99999"

    def test_missing_text_defaults_to_empty_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 1}}
        msg = client._parse_message(payload)
        assert msg.text == ""

    def test_attaches_populated(self):
        client = _make_client()
        attaches = [{"_type": "PHOTO", "url": "http://example.com/img.jpg"}]
        payload = {"chatId": 1, "message": {"attaches": attaches}}
        msg = client._parse_message(payload)
        assert msg.attaches == attaches

    def test_attaches_none_becomes_empty_list(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"attaches": None}}
        msg = client._parse_message(payload)
        assert msg.attaches == []

    def test_link_populated(self):
        client = _make_client()
        link = {"type": "FORWARD", "message": {"text": "original"}}
        payload = {"chatId": 1, "message": {"link": link}}
        msg = client._parse_message(payload)
        assert msg.link == link

    def test_link_none_becomes_empty_dict(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"link": None}}
        msg = client._parse_message(payload)
        assert msg.link == {}

    def test_raw_is_full_payload(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"text": "hi"}, "extra": "data"}
        msg = client._parse_message(payload)
        assert msg.raw is payload

    def test_is_self_false_when_my_id_not_set(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_false_when_sender_differs(self):
        client = _make_client()
        client._my_id = 1
        payload = {"chatId": 1, "message": {"sender": 99}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_true_when_sender_matches_my_id(self):
        client = _make_client()
        client._my_id = 42
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is True

    def test_chat_id_none_when_absent(self):
        client = _make_client()
        payload = {"message": {"text": "no chat id"}}
        msg = client._parse_message(payload)
        assert msg.chat_id is None

    def test_empty_message_dict_returns_none(self):
        # Empty dict is falsy in Python, so _parse_message treats it as absent
        client = _make_client()
        payload = {"chatId": 5, "message": {}}
        msg = client._parse_message(payload)
        assert msg is None


# ---------------------------------------------------------------------------
# MaxClient constructor / basic state
# ---------------------------------------------------------------------------

class TestMaxClientInit:
    def test_token_stored(self):
        c = MaxClient(token="my_token", device_id="dev1")
        assert c.token == "my_token"

    def test_device_id_stored(self):
        c = MaxClient(token="tok", device_id="mydev")
        assert c.device_id == "mydev"

    def test_debug_default_false(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.debug is False

    def test_debug_explicit_true(self):
        c = MaxClient(token="tok", device_id="dev", debug=True)
        assert c.debug is True

    def test_initial_seq_is_zero(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._seq == 0

    def test_initial_my_id_is_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._my_id is None

    def test_ws_url_constant(self):
        assert MaxClient.WS_URL == "wss://api.oneme.ru/websocket"

    def test_heartbeat_sec_constant(self):
        assert MaxClient.HEARTBEAT_SEC == 30

    def test_reconnect_sec_constant(self):
        assert MaxClient.RECONNECT_SEC == 5

    def test_on_disconnect_cb_initial_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._on_disconnect_cb is None

    def test_on_disconnect_decorator_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        @c.on_disconnect
        async def my_handler():
            pass

        assert c._on_disconnect_cb is my_handler

    def test_on_disconnect_returns_function(self):
        c = MaxClient(token="tok", device_id="dev")

        async def my_handler():
            pass

        result = c.on_disconnect(my_handler)
        assert result is my_handler


# ---------------------------------------------------------------------------
# Binary frame codec (protocol v10)
# ---------------------------------------------------------------------------

import msgpack
from app.max_client import (
    decode_frame, encode_frame, profile_contact, _lz4_block_decompress,
)


class TestFrameCodec:
    def test_header_layout(self):
        raw = encode_frame(0, 5, OpCode.HANDSHAKE, {"a": 1})
        body = msgpack.packb({"a": 1})
        assert raw[:10] == bytes([10, 0, 0, 5, 0, 6, 0]) + len(body).to_bytes(3, "big")
        assert raw[10:] == body

    def test_roundtrip(self):
        payload = {"chatId": -70123456789, "text": "привет", "ok": True}
        frame = decode_frame(encode_frame(1, 300, OpCode.SEND_MESSAGE, payload))
        assert frame == {"cmd": 1, "seq": 300, "opcode": 64, "payload": payload}

    def test_empty_payload(self):
        raw = encode_frame(1, 7, OpCode.HEARTBEAT_PING, None)
        assert len(raw) == 10
        assert decode_frame(raw)["payload"] == {}

    def test_bigint_extension(self):
        inner = msgpack.packb(2**62)
        body = msgpack.packb({"id": msgpack.ExtType(1, inner)})
        raw = bytes([10, 0, 0, 1, 0, 128, 0]) + len(body).to_bytes(3, "big") + body
        assert decode_frame(raw)["payload"] == {"id": 2**62}

    def test_numeric_map_keys_become_strings(self):
        body = msgpack.packb({123: "x"}, strict_types=False)
        raw = bytes([10, 1, 0, 1, 0, 19, 0]) + len(body).to_bytes(3, "big") + body
        assert decode_frame(raw)["payload"] == {"123": "x"}

    def test_compressed_payload(self):
        plain = msgpack.packb({"t": "abcdabcdabcd"})
        # literals up to the first "abcd", then an 8-byte match at offset 4, then the tail
        head = plain.index(b"abcd") + 4
        tail = plain[head + 8:]
        block = bytes([(head << 4) | 4]) + plain[:head] + b"\x04\x00" + bytes([len(tail) << 4]) + tail
        raw = bytes([10, 0, 0, 1, 0, 128, 4]) + len(block).to_bytes(3, "big") + block
        assert decode_frame(raw)["payload"] == {"t": "abcdabcdabcd"}

    def test_lz4_overlapping_match(self):
        assert _lz4_block_decompress(bytes([0x12]) + b"a" + b"\x01\x00" + bytes([0x00]), 100) == b"a" * 7

    def test_lz4_rejects_bad_offset(self):
        with pytest.raises(ValueError):
            _lz4_block_decompress(bytes([0x10]) + b"a" + b"\x05\x00", 100)


class TestServerEvents:
    def _client(self):
        c = MaxClient(token="tok", device_id="dev")
        c._send = AsyncMock(return_value=0)
        return c

    async def test_ping_is_answered(self):
        c = self._client()
        await c._handle({"cmd": 0, "seq": 3, "opcode": 1, "payload": {}})
        c._send.assert_awaited_once_with(OpCode.HEARTBEAT_PING, None, cmd=1, seq=3)

    async def test_dispatch_is_acked(self):
        c = self._client()
        payload = {"chatId": 10, "message": {"id": "555", "sender": 1, "text": "hi"}}
        await c._handle({"cmd": 0, "seq": 4, "opcode": 128, "payload": payload})
        c._send.assert_awaited_once_with(
            OpCode.DISPATCH, {"chatId": 10, "messageId": "555"}, cmd=1, seq=4,
        )

    async def test_redelivered_event_is_dropped(self):
        c = self._client()
        c._on_message_cb = AsyncMock()
        c._last_in_seq = 9
        await c._handle({"cmd": 0, "seq": 9, "opcode": 128, "payload": {"chatId": 1, "message": {}}})
        c._send.assert_not_awaited()
        c._on_message_cb.assert_not_called()


class TestProfileContact:
    def test_new_layout(self):
        assert profile_contact({"profile": {"contact": {"id": 432}}})["id"] == 432

    def test_old_layout(self):
        assert profile_contact({"profile": {"id": 42}})["id"] == 42

    def test_missing(self):
        assert profile_contact({}) == {}


class TestSendReply:
    async def test_reply_link_added(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"message": {"id": 1}})
        await c.send_message(42, "hi", reply_to="115000000000000001")
        payload = c.cmd.await_args.args[1]
        assert payload["message"]["link"] == {"type": "REPLY", "messageId": 115000000000000001}

    async def test_no_link_without_reply(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={})
        await c.send_message(42, "hi")
        assert "link" not in c.cmd.await_args.args[1]["message"]


class TestEditMessage:
    async def test_edit_payload(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"message": {"id": 1}})
        await c.edit_message(42, "115000000000000001", "new", elements=[{"type": "STRONG"}])
        op, payload = c.cmd.await_args.args
        assert op == OpCode.EDIT_MESSAGE
        assert payload == {"chatId": 42, "messageId": 115000000000000001, "text": "new",
                           "elements": [{"type": "STRONG"}]}


class TestNativeVoiceAndVideoNote:
    def _client(self, raw_reply):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"info": [{"videoId": 77, "token": "tk", "url": "https://up.example/u"}]})
        c._raw_upload = AsyncMock(return_value=raw_reply)
        return c

    async def test_voice_becomes_audio_attach(self):
        c = self._client({})
        attach = await c.upload_audio(b"ogg", duration_ms=3000)
        wave = attach.pop("wave")
        assert attach == {"_type": "AUDIO", "audioId": 77, "duration": 3000, "token": "tk"}
        assert len(wave) == 80  # MAX rejects voice without a wave
        c.cmd.assert_awaited_once_with(OpCode.MEDIA_UPLOAD_URL, {"count": 1, "type": 2, "uploaderType": 1})
        c._raw_upload.assert_awaited_once_with("https://up.example/u", b"ogg", "voice.ogg", content_range=True)

    async def test_voice_upload_failure(self):
        c = self._client(None)
        assert await c.upload_audio(b"ogg") is None

    async def test_video_note_becomes_round_video(self):
        c = self._client({"thumbhash": "AQID"})
        attach = await c.upload_video_note(b"mp4", duration_ms=2000)
        assert attach == {"_type": "VIDEO", "videoId": 77, "token": "tk", "videoType": 1,
                          "duration": 2000, "thumbhash": b"\x01\x02\x03"}
        c.cmd.assert_awaited_once_with(OpCode.MEDIA_UPLOAD_URL, {"count": 1, "type": 1, "uploaderType": 1})

    async def test_video_note_needs_thumbhash(self):
        c = self._client({})
        assert await c.upload_video_note(b"mp4") is None

    async def test_missing_slot(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"_max_error": {"error": "x"}})
        c._raw_upload = AsyncMock()
        assert await c.upload_audio(b"ogg") is None
        c._raw_upload.assert_not_awaited()

    def test_thumbhash_packs_as_msgpack_bin(self):
        frame = decode_frame(encode_frame(0, 1, OpCode.SEND_MESSAGE, {"t": b"\x01\x02"}))
        assert frame["payload"]["t"] == b"\x01\x02"


from app.max_client import ogg_waveform


def _ogg(packet_sizes):
    """Minimal Ogg stream: OpusHead + OpusTags + audio packets, one page each."""
    out = b""
    for size in [19, 16] + packet_sizes:
        lacing = bytes([255] * (size // 255) + [size % 255])
        out += b"OggS" + bytes(22) + bytes([len(lacing)]) + lacing + bytes(size)
    return out


class TestOggWaveform:
    def test_shape_follows_packet_sizes(self):
        wave = ogg_waveform(_ogg([3] * 40 + [120] * 40), bars=4)
        assert list(wave) == [0, 0, 127, 127]

    def test_long_packets_use_lacing(self):
        wave = ogg_waveform(_ogg([10, 600]), bars=2)
        assert list(wave) == [0, 127]

    def test_unparseable_is_flat(self):
        assert ogg_waveform(b"not ogg") == bytes([31]) * 80

    def test_silence_is_flat(self):
        assert len(set(ogg_waveform(_ogg([3] * 100)))) == 1


class TestDeleteMessage:
    async def test_delete_for_all_payload(self):
        c = MaxClient(token="tok", device_id="dev")
        c.cmd = AsyncMock(return_value={"messageIds": [115000000000000001]})
        await c.delete_message(42, ["115000000000000001"])
        assert c.cmd.await_args.args == (OpCode.DELETE_MESSAGE,
            {"chatId": 42, "messageIds": [115000000000000001], "forMe": False})

    def test_handshake_version_follows_client(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.app_version == "26.11.9"
