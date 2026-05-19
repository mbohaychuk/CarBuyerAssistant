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
# attempts: 1 = first try, 2 = retry-after-rate-limit; we don't retry beyond.
_FIRST_ATTEMPT = 1
_LAST_ATTEMPT = 2


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
                    "type": _BUTTON, "style": _STYLE_DANGER,
                    "label": "\U0001f44e Not interested",
                    "custom_id": f"deal:not_interested:{lot_id}",
                },
            ],
        }
    ]


async def _post_with_retry(
    s: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    log_kwargs: dict[str, object],
) -> bool:
    """POST ``payload`` to ``url`` with one rate-limit retry and one network retry.

    Returns True on HTTP 2xx; False on any unrecoverable error. Structured-log
    context is passed via ``log_kwargs`` (e.g. channel_id, lot_id).

    Rate-limit (429): waits Retry-After once, retries once, then returns False.
    Network error (ClientError / TimeoutError): sleeps once, retries once, then
    returns False.
    Non-2xx non-429: returns False immediately (no retry — caller must not
    re-POST on application errors).
    """
    for attempt in (_FIRST_ATTEMPT, _LAST_ATTEMPT):
        try:
            async with s.post(url, headers=headers, json=payload) as resp:
                if resp.status == 429:  # noqa: PLR2004
                    retry = float(
                        resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S),
                    )
                    log.warning(
                        "discord rate-limited",
                        **log_kwargs, retry_after=retry, attempt=attempt,
                    )
                    if attempt == _FIRST_ATTEMPT:
                        await asyncio.sleep(retry)
                        continue
                    return False
                if 200 <= resp.status < 300:  # noqa: PLR2004
                    log.info("discord message posted", **log_kwargs, status=resp.status)
                    return True
                body = await resp.text()
                log.warning(
                    "discord post failed",
                    **log_kwargs, status=resp.status, body=body[:500],
                )
                return False
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning(
                "discord post network error",
                **log_kwargs, error=str(exc), attempt=attempt,
            )
            if attempt == _LAST_ATTEMPT:
                return False
            await asyncio.sleep(_DEFAULT_RETRY_AFTER_S)
    return False


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
    Omitting ``session`` creates a new ``ClientSession`` per call; only do this
    for one-off use — the notifier worker passes a long-lived session for batches.
    """
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        return False

    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    payload: dict[str, Any] = {
        "content": content,
        "components": _components_for_lot(lot_id),
    }
    log_kwargs: dict[str, object] = {"channel_id": channel_id, "lot_id": lot_id}

    if session is not None:
        return await _post_with_retry(session, url, headers, payload, log_kwargs=log_kwargs)
    async with aiohttp.ClientSession() as s:
        return await _post_with_retry(s, url, headers, payload, log_kwargs=log_kwargs)


async def post_simple_message(
    channel_id: int,
    content: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> bool:
    """POST a button-less message to a Discord channel.

    Mirrors ``post_message`` but omits the action-row components. Used for
    system/needs_plugin alerts that don't need lot-action buttons.

    Returns True on success (HTTP 2xx); False on missing token, HTTP error,
    rate-limit (after one retry), or network error (after one retry).
    """
    if not settings.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not configured")
        return False

    url = f"{_DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    payload: dict[str, Any] = {"content": content}
    log_kwargs: dict[str, object] = {"channel_id": channel_id}

    if session is not None:
        return await _post_with_retry(session, url, headers, payload, log_kwargs=log_kwargs)
    async with aiohttp.ClientSession() as s:
        return await _post_with_retry(s, url, headers, payload, log_kwargs=log_kwargs)
