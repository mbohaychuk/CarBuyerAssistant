"""Direct Discord REST POST for the notifier worker.

Bypasses the gateway (no ``discord.Client`` per notification). The bot worker
runs persistently with the View registered, so button interactions still route
through it; we only need REST to deliver the message + components.
"""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from carbuyer.shared.config import settings
from carbuyer.shared.logging import get_logger

log = get_logger("notifier_post")

_DISCORD_API = "https://discord.com/api/v10"
# Component type IDs from Discord docs.
_ACTION_ROW = 1
_BUTTON = 2
# Button styles: 1=PRIMARY 2=SECONDARY 3=SUCCESS 4=DANGER 5=LINK.
_STYLE_SUCCESS = 3
_STYLE_SECONDARY = 2
_STYLE_DANGER = 4
# Discord rate-limit fallback when the Retry-After header is absent.
_DEFAULT_RETRY_AFTER_S = 1.0


def _components_for_lot(lot_id: int) -> list[dict[str, Any]]:
    return [
        {
            "type": _ACTION_ROW,
            "components": [
                {
                    "type": _BUTTON, "style": _STYLE_SUCCESS,
                    "label": "\U0001f44d Interested",
                    "custom_id": f"deal:interested:{lot_id}",
                },
                {
                    "type": _BUTTON, "style": _STYLE_SECONDARY,
                    "label": "\U0001f914 Maybe",
                    "custom_id": f"deal:maybe:{lot_id}",
                },
                {
                    "type": _BUTTON, "style": _STYLE_DANGER,
                    "label": "\U0001f44e Not interested",
                    "custom_id": f"deal:not_interested:{lot_id}",
                },
            ],
        }
    ]


async def post_message(
    channel_id: int,
    content: str,
    lot_id: int,
    *,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """POST a message with action-row components to a Discord channel.

    Returns True on success (HTTP 2xx). Logs and returns False on:
      - missing bot token (cannot authenticate)
      - any HTTP error (4xx, 5xx, network)
      - Discord rate-limit (429); we wait Retry-After once and retry once.

    Pass ``session`` to reuse a long-lived aiohttp.ClientSession (preferred for
    batches). When omitted, a fresh session is opened per call.
    """
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        return False

    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    payload = {"content": content, "components": _components_for_lot(lot_id)}

    async def _do(s: aiohttp.ClientSession) -> bool:
        for attempt in (1, 2):
            try:
                async with s.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 429:  # noqa: PLR2004
                        retry = float(
                            resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                        )
                        log.warning(
                            "discord rate-limited",
                            channel_id=channel_id, retry_after=retry, attempt=attempt,
                        )
                        if attempt == 1:
                            await asyncio.sleep(retry)
                            continue
                        return False
                    if 200 <= resp.status < 300:  # noqa: PLR2004
                        log.info(
                            "discord message posted",
                            channel_id=channel_id, lot_id=lot_id, status=resp.status,
                        )
                        return True
                    body = await resp.text()
                    log.warning(
                        "discord post failed",
                        channel_id=channel_id, lot_id=lot_id,
                        status=resp.status, body=body[:500],
                    )
                    return False
            except (aiohttp.ClientError, TimeoutError) as exc:
                log.warning(
                    "discord post network error",
                    channel_id=channel_id, lot_id=lot_id,
                    error=str(exc), attempt=attempt,
                )
                if attempt == 2:  # noqa: PLR2004
                    return False
                await asyncio.sleep(_DEFAULT_RETRY_AFTER_S)
        return False

    if session is not None:
        return await _do(session)
    async with aiohttp.ClientSession() as s:
        return await _do(s)
