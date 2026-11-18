"""Track the MAX web client version the bridge impersonates.

MAX bakes the WebSocket URL and app version into its JS bundle, e.g.
``ld=`wss://api.oneme.ru/websocket`,ud=`26.11.9```. Reading them at startup
lets the handshake follow minor client updates, and a changed URL is the early
sign of a protocol change like the one that broke the bridge in Nov 2026.
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from urllib.parse import urljoin

import aiohttp

log = logging.getLogger(__name__)

WEB_URL = "https://web.max.ru/"
CHECK_EVERY_SEC = 6 * 3600
_SCRIPT_RE = re.compile(r"""(?:src|href)=["']([^"']+\.js)["']""")
_VERSION_RE = re.compile(r"""["'`](wss://[^"'`\s]+/websocket)["'`]\s*,\s*[\w$]+\s*=\s*["'`](\d+\.\d+\.\d+)["'`]""")


def parse_bundle(js: str) -> tuple[str, str] | None:
    """(ws_url, app_version) from a JS bundle, or None."""
    m = _VERSION_RE.search(js)
    return (m.group(1), m.group(2)) if m else None


async def detect(session: aiohttp.ClientSession, headers: dict | None = None) -> tuple[str, str] | None:
    """Fetch web.max.ru and return (ws_url, app_version) from its scripts."""
    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with session.get(WEB_URL, headers=headers, timeout=timeout) as resp:
            html = await resp.text()
        for src in dict.fromkeys(_SCRIPT_RE.findall(html)):
            async with session.get(urljoin(WEB_URL, src), headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                found = parse_bundle(await resp.text())
            if found:
                return found
    except Exception:
        log.warning("MAX web client version check failed", exc_info=True)
        return None
    log.warning("MAX web client version not found in %s scripts", WEB_URL)
    return None


def describe_change(known_url: str, known_version: str, url: str, version: str) -> str | None:
    """Telegram warning (HTML) for a changed client, or None if nothing changed."""
    if url != known_url:
        return (
            "🚨 <b>MAX сменил адрес веб-сокета</b>\n"
            f"<code>{escape(known_url)}</code> → <code>{escape(url)}</code>\n"
            "Скорее всего, поменялся протокол — мост может перестать получать сообщения."
        )
    if version != known_version:
        return (
            "ℹ️ <b>MAX обновил веб-клиент</b>: "
            f"<code>{escape(known_version)}</code> → <code>{escape(version)}</code>\n"
            "Мост подключается с новой версией. Если что-то перестанет работать — дело в обновлении MAX."
        )
    return None


async def watch(client, notify, headers: dict | None = None, every: float = CHECK_EVERY_SEC) -> None:
    """Keep ``client.app_version`` current and report changes via ``notify(text)``."""
    known = (client.WS_URL, client.app_version)
    async with aiohttp.ClientSession() as session:
        while True:
            found = await detect(session, headers)
            if found:
                text = describe_change(*known, *found)
                if text:
                    log.warning("MAX web client changed: %s %s → %s %s", *known, *found)
                    client.app_version = found[1]  # used on the next handshake
                    try:
                        await notify(text)
                    except Exception:
                        log.warning("Could not send MAX version notice", exc_info=True)
                    known = found
                else:
                    log.info("MAX web client: %s (%s)", found[1], found[0])
            await asyncio.sleep(every)
