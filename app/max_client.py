import asyncio
import base64
import json
import logging
import os
import random
import struct
import time
from urllib.parse import quote, urlparse
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import aiohttp
import msgpack

log = logging.getLogger(__name__)

DEBUG_DIR = "debug"

# ── binary frame codec (web client protocol v10, since Nov 2026) ──
#
# Frame = 10-byte header + msgpack payload:
#   u8 proto version (10) | u8 cmd | i16 seq | i16 opcode
#   u8 compression factor (0 = none, else payload is a raw LZ4 block and
#      decompressed size <= len * factor) | u24 payload length
# msgpack ext type 1 carries int64 values (ids) as a nested msgpack int.

PROTO_VER = 10
APP_VERSION = "26.11.9"
_HEADER = struct.Struct(">BBhhB")
HEADER_LEN = 10
MAX_SEQ = 32767
_EXT_BIGINT = 1


def _lz4_block_decompress(src: bytes, max_size: int) -> bytes:
    """Decode a raw LZ4 block (no frame header)."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break  # last sequence has literals only
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        mlen = (token & 15) + 4
        if (token & 15) == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        start = len(dst) - offset
        if offset == 0 or start < 0 or len(dst) + mlen > max_size:
            raise ValueError("corrupt LZ4 block")
        if offset >= mlen:
            dst += dst[start:start + mlen]
        else:  # overlapping match repeats the tail
            for k in range(mlen):
                dst.append(dst[start + k])
    return bytes(dst)


def _ext_hook(code: int, data: bytes):
    if code == _EXT_BIGINT:
        return msgpack.unpackb(data)
    return msgpack.ExtType(code, data)


def _str_keys(pairs):
    # The web client sees maps as JS objects, so numeric keys become strings;
    # keep that so payload["<chatId>"] lookups behave as with the old JSON API.
    return {k if isinstance(k, str) else str(k): v for k, v in pairs}


def encode_frame(cmd: int, seq: int, opcode: int, payload: Any) -> bytes:
    body = b"" if payload is None else msgpack.packb(payload, use_bin_type=True)
    n = len(body)
    return _HEADER.pack(PROTO_VER, cmd, seq, opcode, 0) + n.to_bytes(3, "big") + body


def decode_frame(raw: bytes) -> dict:
    ver, cmd, seq, opcode, factor = _HEADER.unpack_from(raw)
    if ver != PROTO_VER:
        log.warning("Unexpected proto version %d for op=%d", ver, opcode)
    n = int.from_bytes(raw[7:10], "big")
    frame = {"cmd": cmd, "seq": seq, "opcode": opcode, "payload": {}}
    if n <= 0:
        return frame
    body = raw[HEADER_LEN:HEADER_LEN + n]
    if len(body) != n:
        raise ValueError(f"frame length mismatch: header={n} actual={len(body)}")
    if factor > 0:
        body = _lz4_block_decompress(body, n * factor)
    frame["payload"] = msgpack.unpackb(
        body, raw=False, strict_map_key=False,
        ext_hook=_ext_hook, object_pairs_hook=_str_keys,
    )
    return frame


WAVE_BARS = 80
WAVE_MAX = 127


def ogg_waveform(data: bytes, bars: int = WAVE_BARS) -> bytes:
    """Rough voice waveform for a MAX AUDIO attach, without decoding audio.

    MAX rejects voice attaches that lack a wave (with a misleading
    "video.not.supported"). Telegram voice notes are Ogg/Opus, where each
    packet covers ~20 ms and its byte size grows with loudness, so packet
    sizes bucketed into ``bars`` columns and scaled to 0..127 give a usable
    shape. Falls back to a flat wave if the container can't be parsed.
    """
    sizes: list[int] = []
    pos, cur = 0, 0
    while pos + 27 <= len(data) and data[pos:pos + 4] == b"OggS":
        nseg = data[pos + 26]
        lacing = data[pos + 27:pos + 27 + nseg]
        pos += 27 + nseg
        for lace in lacing:
            cur += lace
            if lace < 255:  # packet ends on a segment shorter than 255
                sizes.append(cur)
                cur = 0
        pos += sum(lacing)
    sizes = sizes[2:]  # OpusHead + OpusTags
    if not sizes:
        return bytes([WAVE_MAX // 4]) * bars
    step = len(sizes) / bars
    cols = []
    for i in range(bars):
        chunk = sizes[int(i * step):max(int((i + 1) * step), int(i * step) + 1)]
        cols.append(sum(chunk) / len(chunk) if chunk else 0)
    lo, hi = min(cols), max(cols)
    if hi - lo < 1e-9:
        return bytes([WAVE_MAX // 4]) * bars
    return bytes(int((c - lo) / (hi - lo) * WAVE_MAX) for c in cols)


def profile_contact(snapshot: dict) -> dict:
    """AUTH_SNAPSHOT profile; since Nov 2026 the user data sits under profile.contact."""
    profile = snapshot.get("profile") or {}
    return profile.get("contact") or profile


def _log_task_exception(task: "asyncio.Task") -> None:
    """Done-callback that logs any exception raised by a fire-and-forget task."""
    if not task.cancelled() and task.exception() is not None:
        log.exception("Unhandled exception in background task", exc_info=task.exception())

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_WS_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_HTTP_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    # Override the browser default to avoid brotli/zstd: aiohttp ships without
    # these codecs by default, and the MAX upload server returns its JSON body
    # brotli-encoded when "br" is advertised.
    "Accept-Encoding": "gzip, deflate",
}


class OpCode(IntEnum):
    HEARTBEAT_PING = 1
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    LOGOUT = 20
    STICKER_STORE = 27
    ASSET_GET = 28
    FAVORITE_STICKER = 29
    CONTACT_GET = 32
    CONTACT_PRESENCE = 35
    CHAT_GET = 48
    CHAT_HISTORY = 49
    SEND_MESSAGE = 64
    ATTACH_TYPING = 65        # "I'm uploading <type> in this chat"
    DELETE_MESSAGE = 66
    EDIT_MESSAGE = 67
    PHOTO_UPLOAD_URL = 80     # get URL for photo upload
    MEDIA_UPLOAD_URL = 82     # slot for direct voice (type 2) / video note (type 1) upload
    VIDEO_PLAY = 83           # resolve a video attachment to an MP4 URL
    AUDIO_UPLOAD_URL = 86     # get URL for voice/audio upload (experimental)
    FILE_UPLOAD_URL = 87      # get URL for file upload
    DISPATCH = 128
    UPLOAD_READY = 136        # server says an uploaded file/video is processed
    READ_MARK_PUSH = 130      # another participant advanced their read marker
    AUDIO_PLAY = 301          # resolve a voice attachment to an OGG URL


@dataclass
class MaxMessage:
    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class MaxClient:
    WS_URL = "wss://api.oneme.ru/websocket"
    HEARTBEAT_SEC = 30
    RECONNECT_SEC = 5

    def __init__(self, token: str, device_id: str, chat_ids: str | None = None, debug: bool = False, exclude_chat_ids: str | None = None):
        self.token = token
        self.device_id = device_id
        self.debug = debug
        # Kept current from web.max.ru by app.max_version.watch.
        self.app_version = APP_VERSION
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._last_in_seq = 0
        self._my_id = None
        self._on_ready_cb = None
        self._on_message_cb = None
        self._on_read_mark_cb = None
        self._heartbeat_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatch_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._file_pending: dict[int, asyncio.Future] = {}
        self._on_disconnect_cb = None
        self.chat_ids = set(map(int, map(str.strip, chat_ids.split(',')))) if chat_ids else set()
        self.exclude_chat_ids = set(map(int, map(str.strip, exclude_chat_ids.split(',')))) if exclude_chat_ids else set()

    # ── decorator API ──────────────────────────────────────────────

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_read_mark(self, func):
        self._on_read_mark_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    # ── transport ──────────────────────────────────────────────────

    async def _send(self, opcode: int, payload: dict | None, cmd: int = 0, seq: int | None = None) -> int:
        """Send a frame. With seq=None a new request seq is allocated; pass the
        server's seq (and cmd=1) to acknowledge a server-initiated event."""
        if not self._ws or self._ws.closed:
            return -1
        if seq is None:
            seq = self._seq
            if seq >= MAX_SEQ:
                # seq is int16 on the wire — the web client reconnects here too
                log.info("seq limit reached, reconnecting")
                await self._ws.close()
                return -1
            self._seq += 1
        log.debug(">>> SEND op=%d cmd=%d seq=%d | %s", opcode, cmd, seq,
                  json.dumps(payload, ensure_ascii=False, default=str)[:800])
        await self._ws.send_bytes(encode_frame(cmd, seq, opcode, payload))
        return seq

    async def cmd(self, opcode: int, payload: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response (cmd=1 with same seq)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        seq = await self._send(opcode, payload)
        self._pending[seq] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("cmd timeout: op=%d seq=%d", opcode, seq)
            return {}
        finally:
            self._pending.pop(seq, None)

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            try:
                if self._ws and not self._ws.closed:
                    await self._send(OpCode.HEARTBEAT_PING, {"interactive": False})
                else:
                    break
            except Exception:
                log.exception("Heartbeat error, stopping heartbeat loop")
                break

    # ── main loop ──────────────────────────────────────────────────

    async def run(self):
        if self.debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            self._session = session
            while True:
                try:
                    log.info("Connecting to %s ...", self.WS_URL)
                    async with session.ws_connect(
                        self.WS_URL, headers=_WS_HEADERS
                    ) as ws:
                        self._ws = ws
                        self._seq = 0
                        self._last_in_seq = 0
                        self._pending.clear()

                        log.info("Connected. Sending handshake...")
                        await self._send(
                            OpCode.HANDSHAKE,
                            {
                                "userAgent": {
                                    "deviceType": "WEB",
                                    "locale": "ru",
                                    "deviceLocale": "ru",
                                    "osVersion": "Windows 10.0",
                                    "deviceName": "Google Chrome 131.0.0.0",
                                    "headerUserAgent": _USER_AGENT,
                                    "isPwa": False,
                                    "appVersion": self.app_version,
                                    "screen": "1080x1920 1.0x",
                                    "timezone": "Europe/Moscow",
                                },
                                "deviceId": self.device_id,
                                "clientSessionId": int(time.time() * 1000),
                            },
                        )

                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop()
                        )

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                try:
                                    frame = decode_frame(msg.data)
                                except Exception:
                                    log.exception("Bad frame (%d bytes): %s", len(msg.data), msg.data[:16].hex())
                                    continue
                                await self._handle(frame)
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                log.warning("Unexpected text frame: %s", msg.data[:300])
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning("WebSocket closed/error: %s", msg.type)
                                break
                        log.warning(
                            "WebSocket ended: close_code=%s exception=%s",
                            ws.close_code,
                            type(ws.exception()).__name__ if ws.exception() else "none",
                        )

                except Exception:
                    log.exception("Connection error")

                finally:
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.cancel()
                    self._pending.clear()

                if self._on_disconnect_cb:
                    try:
                        await self._on_disconnect_cb()
                    except Exception:
                        log.exception("on_disconnect callback error")

                log.info("Reconnecting in %ds...", self.RECONNECT_SEC)
                await asyncio.sleep(self.RECONNECT_SEC)

    # ── event dispatcher ───────────────────────────────────────────

    async def _handle(self, data: dict):
        op = data.get("opcode")
        cmd = data.get("cmd")
        seq = data.get("seq")
        payload = data.get("payload") or {}

        if cmd == 0:
            # Server-initiated: drop redeliveries, answer pings, ack messages
            # (mirrors the web client; unacked events get resent).
            if seq <= self._last_in_seq:
                return
            self._last_in_seq = seq
            if op == OpCode.HEARTBEAT_PING:
                await self._send(OpCode.HEARTBEAT_PING, None, cmd=1, seq=seq)
                return
            if op == OpCode.DISPATCH:
                await self._send(
                    OpCode.DISPATCH,
                    {"chatId": payload.get("chatId"),
                     "messageId": (payload.get("message") or {}).get("id")},
                    cmd=1, seq=seq,
                )

        # cmd=1 is a response to our request — resolve the pending future
        if cmd == 1 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(payload)
            if op not in (OpCode.HANDSHAKE, OpCode.AUTH_SNAPSHOT):
                log.debug("<<< RESP  op=%-4s seq=%s", op, seq)

        # cmd=3 is an error response
        elif cmd == 3 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result({"_max_error": payload})
            log.warning("<<< ERROR op=%-4s seq=%s | %s", op, seq, payload)

        # server-initiated events — not a reply to one of our requests
        else:
            payload_preview = json.dumps(payload, ensure_ascii=False, default=str)
            if len(payload_preview) > 3000:
                payload_preview = payload_preview[:3000] + "…"

            if op == OpCode.HANDSHAKE and cmd == 1:
                log.info("Handshake OK → sending auth token...")
                await self._send(
                    OpCode.AUTH_SNAPSHOT,
                    {
                        "chatsCount": 100,
                        "interactive": True,
                        "token": self.token,
                    },
                )

            elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
                self._my_id = profile_contact(payload).get("id")
                log.info("Authorized! my_id=%s", self._my_id)
                if self.debug:
                    self._dump_json("snapshot.json", payload)

                if self._on_ready_cb:
                    await self._on_ready_cb(payload)

            elif op == OpCode.DISPATCH:
                self._dispatch_counter += 1
                if self.debug and self._dispatch_counter <= 20:
                    self._dump_json(
                        f"dispatch_{self._dispatch_counter:04d}.json", payload
                    )

                if self._on_message_cb:
                    msg = self._parse_message(payload)
                    if msg is not None and msg.chat_id not in self.exclude_chat_ids and ((not self.chat_ids) or (msg.chat_id in self.chat_ids)):
                        task = asyncio.create_task(self._on_message_cb(msg))
                        task.add_done_callback(_log_task_exception)

            elif op == OpCode.READ_MARK_PUSH and cmd == 0:
                if self._on_read_mark_cb:
                    task = asyncio.create_task(self._on_read_mark_cb(payload))
                    task.add_done_callback(_log_task_exception)

            elif op == OpCode.UPLOAD_READY:
                # Server confirms an uploaded file/video finished server-side processing.
                file_id = payload.get("fileId")
                if file_id is not None:
                    fut = self._file_pending.pop(int(file_id), None)
                    if fut and not fut.done():
                        fut.set_result(payload)
                log.debug("UPLOAD_READY op=136: %s", payload)

            elif op in (OpCode.HEARTBEAT_PING,):
                log.debug("Heartbeat op=%s", op)

            elif cmd not in (1, 3):
                log.info("<<< EVENT op=%-4s cmd=%-3s | %s", op, cmd, payload_preview[:500])

    # ── WebSocket RPC: fetch contacts ──────────────────────────────

    async def fetch_contacts(self, contact_ids: list[int]) -> dict:
        """Fetch contact info via WS opcode 32. Returns raw response payload."""
        if not contact_ids:
            return {}
        resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": contact_ids})
        if self.debug:
            self._dump_json("contacts_response.json", resp)
        log.info("fetch_contacts(%s) → keys: %s", contact_ids, list(resp.keys()))
        return resp

    async def fetch_history(self, chat_id: int, before_ms: int, count: int = 100) -> list[dict] | None:
        """Read a page of MAX chat history; None means the request failed."""
        response = await self.cmd(
            OpCode.CHAT_HISTORY,
            {"chatId": chat_id, "from": before_ms, "forward": 0,
             "backward": count, "getMessages": True, "getChat": False,
             "interactive": False},
            timeout=20,
        )
        if not response or "_max_error" in response or not isinstance(response.get("messages"), list):
            return None
        return response["messages"]

    def parse_history_message(self, chat_id: int, message: dict) -> MaxMessage | None:
        return self._parse_message({"chatId": chat_id, "message": message})

    async def send_message(self, chat_id, text: str = "", elements=None,
                            attaches=None, reply_to=None) -> dict:
        """Send a message to a Max chat. Returns the server response.

        Both ``elements`` (text formatting) and ``attaches`` (photos, files,
        voice, ...) are optional. Pass an empty ``text`` together with an
        attach to send a media-only message. ``reply_to`` is a MAX message id
        to quote (a reply within the same chat).
        """
        if elements is None:
            elements = []
        if attaches is None:
            attaches = []
        cid = int(time.time() * 1000) * 1000 + random.randint(0, 999)
        message = {"text": text, "cid": cid, "elements": elements}
        if attaches:
            message["attaches"] = attaches
        if reply_to:
            mid = str(reply_to)
            message["link"] = {"type": "REPLY", "messageId": int(mid) if mid.isdigit() else mid}
        resp = await self.cmd(
            OpCode.SEND_MESSAGE,
            {
                "chatId": chat_id,
                "message": message,
                "notify": True,
            },
        )
        ok = bool(resp) and "_max_error" not in resp
        log.info("send_message(chat=%s, attaches=%d, reply_to=%s) → %s",
                 chat_id, len(attaches), reply_to, "OK" if ok else "FAIL")
        return resp

    async def edit_message(self, chat_id, message_id, text: str, elements=None) -> dict:
        """Edit the text of one of our messages; attachments stay as they are."""
        mid = str(message_id)
        resp = await self.cmd(
            OpCode.EDIT_MESSAGE,
            {
                "chatId": chat_id,
                "messageId": int(mid) if mid.isdigit() else mid,
                "text": text,
                "elements": elements or [],
            },
        )
        ok = bool(resp) and "_max_error" not in resp
        log.info("edit_message(chat=%s, msg=%s) → %s", chat_id, message_id, "OK" if ok else "FAIL")
        return resp

    async def delete_message(self, chat_id, message_ids, for_all: bool = True) -> dict:
        """Delete MAX messages; ``for_all`` removes them for every participant."""
        ids = [int(m) if str(m).isdigit() else m for m in message_ids]
        resp = await self.cmd(
            OpCode.DELETE_MESSAGE,
            {"chatId": chat_id, "messageIds": ids, "forMe": not for_all},
        )
        ok = bool(resp) and "_max_error" not in resp
        log.info("delete_message(chat=%s, msgs=%s, for_all=%s) → %s",
                 chat_id, ids, for_all, "OK" if ok else "FAIL")
        return resp

    # ── media upload ───────────────────────────────────────────────

    async def upload_photo(self, data: bytes, chat_id=None,
                            filename: str = "image.jpg",
                            mimetype: str = "image/jpeg") -> dict | None:
        """Upload a photo and return an attach dict for send_message.

        Protocol (reverse-engineered, see nsdkinx/vkmax):
          1. opcode 80 → response.payload.url
          2. (optional) opcode 65 → 'I'm uploading PHOTO' indicator
          3. multipart POST file to URL → JSON {'photos': {key: {'token': ...}}}
          4. token → {"_type": "PHOTO", "photoToken": token}
        """
        resp = await self.cmd(OpCode.PHOTO_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("Photo upload URL request failed: %s", resp)
            return None
        url = resp.get("url")
        if not url:
            log.error("Photo upload response missing 'url': %s", resp)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": "PHOTO"})

        body = await self._http_upload(url, data, filename, mimetype,
                                        expect_json=True)
        if not isinstance(body, dict):
            return None

        photos = body.get("photos") or {}
        if not photos:
            log.error("Photo upload OK but no 'photos' in response: %s", body)
            return None
        first = next(iter(photos.values()))
        token = first.get("token") if isinstance(first, dict) else None
        if not token:
            log.error("Photo upload response missing token: %s", body)
            return None
        return {"_type": "PHOTO", "photoToken": token}

    async def open_by_link(self, link: str) -> dict:
        """Resolve a max.ru invite link via opcode 57.

        Works for ``/join/<token>`` (group / channel invite — chat namespace).
        ``/u/<token>`` (user share link) needs a different, undocumented
        opcode that we haven't reverse-engineered yet — server returns
        ``not.found`` for chat-namespace lookup.
        """
        resp = await self.cmd(57, {"link": link})
        log.info("open_by_link(%s) → %s",
                 link[:60], str(resp)[:300] if resp else resp)
        return resp

    async def download_audio_url(self, audio_id, chat_id, message_id,
                                  token: str | None = None) -> str | None:
        """Resolve a MAX voice attachment through AUDIO_PLAY (opcode 301)."""
        try:
            payload = {
                "audioId": int(audio_id),
                "chatId": int(chat_id),
                "messageId": int(message_id),  # int64 in the v10 protocol
            }
        except (TypeError, ValueError):
            return None
        if token:
            payload["token"] = token
        resp = await self.cmd(OpCode.AUDIO_PLAY, payload)
        if not resp or "_max_error" in resp:
            log.warning("AUDIO_PLAY did not return an audio URL")
            return None
        url = resp.get("opus") or resp.get("url")
        return url if self._is_max_media_url(url) else None

    async def download_video_url(self, video_id, chat_id, message_id,
                                  token: str | None = None) -> str | None:
        """Resolve a MAX video attachment through VIDEO_PLAY (opcode 83)."""
        try:
            payload = {
                "videoId": int(video_id),
                "chatId": int(chat_id),
                "messageId": int(message_id),  # int64 in the v10 protocol
            }
        except (TypeError, ValueError):
            return None
        if token:
            payload["token"] = token
        resp = await self.cmd(OpCode.VIDEO_PLAY, payload)
        if not resp or "_max_error" in resp:
            log.warning("VIDEO_PLAY did not return a video URL")
            return None
        options = []
        for key, value in resp.items():
            if isinstance(key, str) and key.startswith("MP4_") and self._is_max_media_url(value):
                try:
                    options.append((int(key[4:]), value))
                except ValueError:
                    pass
        if options:
            return max(options)[1]
        url = resp.get("url") or resp.get("dynamicUrl")
        return url if self._is_max_media_url(url) else None

    @staticmethod
    def _is_max_media_url(url) -> bool:
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and (
            host.endswith(".oneme.ru") or host.endswith(".okcdn.ru")
        )

    async def _media_upload_slot(self, media_type: int) -> dict | None:
        """MEDIA_UPLOAD_URL (opcode 82) → {videoId, token, url} for direct upload."""
        resp = await self.cmd(OpCode.MEDIA_UPLOAD_URL,
                              {"count": 1, "type": media_type, "uploaderType": 1})
        info = (resp or {}).get("info") or []
        slot = info[0] if info and isinstance(info[0], dict) else None
        if not slot or not slot.get("url") or slot.get("videoId") is None:
            log.error("Media upload slot request failed (type=%d): %s", media_type, resp)
            return None
        return slot

    async def upload_audio(self, data: bytes, chat_id=None,
                            filename: str = "voice.ogg",
                            mimetype: str = "audio/ogg",
                            duration_ms: int = 0,
                            timeout: float = 60.0) -> dict | None:
        """Upload an OGG/Opus voice note; returns a native AUDIO attach.

        Mirrors the web client: an opcode-82 slot (type 2), then a raw POST
        of the .ogg to the slot URL. ``.ogg`` uploads are direct, so the
        attach is usable as soon as the POST succeeds.
        """
        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING, {"chatId": chat_id, "type": "AUDIO"})
        slot = await self._media_upload_slot(2)
        if not slot:
            return None
        if await self._raw_upload(slot["url"], data, filename, content_range=True) is None:
            return None
        return {"_type": "AUDIO", "audioId": slot["videoId"],
                "duration": int(duration_ms), "wave": ogg_waveform(data),
                "token": slot.get("token")}

    async def upload_video_note(self, data: bytes, chat_id=None,
                                 duration_ms: int = 0) -> dict | None:
        """Upload a round video message; returns a VIDEO attach with videoType=1.

        Opcode-82 slot (type 1), then the web client's
        "video-message-direct" POST, whose JSON reply must carry a thumbhash.
        """
        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING, {"chatId": chat_id, "type": "VIDEO_MSG"})
        slot = await self._media_upload_slot(1)
        if not slot:
            return None
        body = await self._raw_upload(slot["url"], data, "video_note.mp4", content_range=False)
        thumbhash = (body or {}).get("thumbhash")
        if not thumbhash:
            log.error("Video note upload returned no thumbhash: %s", body)
            return None
        try:
            thumbhash_bytes = base64.b64decode(thumbhash)
        except (ValueError, TypeError):
            log.error("Video note upload returned a bad thumbhash: %r", thumbhash)
            return None
        return {"_type": "VIDEO", "videoId": slot["videoId"], "token": slot.get("token"),
                "videoType": 1, "duration": int(duration_ms), "thumbhash": thumbhash_bytes}

    async def upload_file(self, data: bytes, chat_id=None,
                           filename: str = "file.bin",
                           mimetype: str = "application/octet-stream",
                           attach_type: str = "FILE",
                           timeout: float = 60.0) -> dict | None:
        """Upload a generic file (used for voice, audio, documents, video).

        Unlike photos, the server processes the file asynchronously and only
        then sends an opcode-136 UPLOAD_READY event. We wait for that event
        before returning the attach dict — otherwise the SEND_MESSAGE that
        follows would reference a file the server hasn't finished ingesting.
        """
        resp = await self.cmd(OpCode.FILE_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("File upload URL request failed: %s", resp)
            return None
        info_list = resp.get("info") or []
        if not info_list:
            log.error("File upload response missing 'info': %s", resp)
            return None
        info = info_list[0]
        url = info.get("url")
        file_id = info.get("fileId")
        if not url or file_id is None:
            log.error("File upload info missing url/fileId: %s", info)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": attach_type})

        # Register the pending future BEFORE the POST so the server's reply
        # (which may arrive before our POST coroutine resumes) is not lost.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._file_pending[int(file_id)] = fut

        ok = await self._http_upload(url, data, filename, mimetype,
                                      expect_json=False)
        if not ok:
            self._file_pending.pop(int(file_id), None)
            return None

        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._file_pending.pop(int(file_id), None)
            log.warning("File upload server processing timed out (fileId=%s)",
                        file_id)
            return None

        return {"_type": attach_type, "fileId": file_id}

    async def _http_upload(self, url: str, data: bytes, filename: str,
                            mimetype: str, expect_json: bool):
        """POST multipart bytes to an upload URL. Returns parsed JSON if
        ``expect_json`` is True, otherwise True on HTTP 200, or None on error.
        """
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            form = aiohttp.FormData()
            form.add_field("file", data, filename=filename, content_type=mimetype)
            async with session.post(
                url, headers=_HTTP_HEADERS, data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:500]
                    log.error("Upload POST failed: HTTP %d — %s",
                              resp.status, body)
                    return None
                if expect_json:
                    try:
                        return await resp.json()
                    except Exception:
                        log.exception("Upload response is not JSON")
                        return None
                # consume body to release connection
                await resp.read()
                return True
        except Exception:
            log.exception("Upload POST error at %s", urlparse(url).hostname)
            return None
        finally:
            if close_after:
                await session.close()

    async def _raw_upload(self, url: str, data: bytes, filename: str,
                           content_range: bool) -> dict | None:
        """POST raw bytes (not multipart) to a media upload URL.

        Returns the parsed JSON reply ({} when the body is empty or not JSON)
        on HTTP 200/201, or None on error.
        """
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        headers = {
            **_HTTP_HEADERS,
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename={quote(filename)}",
        }
        if content_range:
            headers["Content-Range"] = f"bytes 0-{len(data) - 1}/{len(data)}"
        try:
            async with session.post(
                url, headers=headers, data=data,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                text = await resp.text()
                if resp.status not in (200, 201):
                    log.error("Raw upload failed: HTTP %d — %s", resp.status, text[:500])
                    return None
                try:
                    body = json.loads(text) if text.strip() else {}
                except ValueError:
                    body = {}
                if isinstance(body, dict) and "error_code" in body:
                    log.error("Raw upload rejected: %s", text[:500])
                    return None
                return body if isinstance(body, dict) else {}
        except Exception:
            log.exception("Raw upload error at %s", urlparse(url).hostname)
            return None
        finally:
            if close_after:
                await session.close()

    async def download_file(self, url: str) -> bytes | None:
        """Download a file by URL, returning raw bytes or None on failure."""
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            async with session.get(
                url, headers=_HTTP_HEADERS,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    log.info("Downloaded from %s (%d bytes)", urlparse(url).hostname, len(data))
                    return data
                log.warning("Download failed at %s — HTTP %d", urlparse(url).hostname, resp.status)
        except Exception:
            log.exception("Download error at %s", urlparse(url).hostname)
        finally:
            if close_after:
                await session.close()
        return None

    # ── message parsing ────────────────────────────────────────────

    def _parse_message(self, payload: dict) -> MaxMessage | None:
        msg_body = payload.get("message")
        if not msg_body or not isinstance(msg_body, dict):
            return None

        msg = MaxMessage(
            chat_id=payload.get("chatId"),
            sender_id=msg_body.get("sender"),
            text=msg_body.get("text", ""),
            timestamp=msg_body.get("time"),
            message_id=str(msg_body.get("id", "")),
            attaches=msg_body.get("attaches") or [],
            link=msg_body.get("link") or {},
            raw=payload,
        )

        if self._my_id and msg.sender_id == self._my_id:
            msg.is_self = True

        return msg

    # ── debug helpers ──────────────────────────────────────────────

    @staticmethod
    def _dump_json(filename: str, data: dict) -> None:
        path = os.path.join(DEBUG_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            log.info("Dumped %s (%d bytes)", path, os.path.getsize(path))
        except Exception:
            log.exception("Failed to dump %s", path)
