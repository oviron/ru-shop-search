"""Tier 1 for shops that check the TLS fingerprint: one request through curl_cffi as Chrome.

``impersonated_get``/``impersonated_post`` never retry or follow redirects: the
caller sees every status and ``Location``. ``shop_request`` adds the shop rules on
top: pacing, the stop check, and a stop on the first refusal. Needs the
``mcp-core[impersonate]`` extra.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as curl_requests

from mcp_core.errors import TransportDownError
from mcp_core.pacing import Pacer
from mcp_core.redact import redact_error_text

REFUSAL_STATUSES = frozenset({401, 403, 418, 429})
TRANSIENT_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True)
class Reply:
    status: int
    text: str | None
    err: str | None
    location: str = ""


def decode_body(body: bytes, content_type: str) -> str:
    """Decode by the Content-Type charset, UTF-8 when it names none or an unknown one."""
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "")
    try:
        return body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


async def _send(
    send: Callable[..., Any],
    url: str,
    *,
    timeout_s: float,
    wall_timeout_s: float,
    max_bytes: int,
    profile: str,
    **kwargs: Any,
) -> Reply:
    def _fetch() -> Reply:
        resp = send(url, impersonate=profile, timeout=timeout_s, allow_redirects=False, stream=True, **kwargs)
        location = str(resp.headers.get("location") or "")
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content():
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return Reply(resp.status_code, None, f"network: body exceeded {max_bytes} bytes", location)
            chunks.append(chunk)
        text = decode_body(b"".join(chunks), str(resp.headers.get("content-type") or ""))
        return Reply(resp.status_code, text, None, location)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=wall_timeout_s)
    except TimeoutError:
        return Reply(0, None, f"timeout: no response within {wall_timeout_s}s")
    except Exception as exc:  # curl_cffi raises its own RequestsError hierarchy
        return Reply(0, None, f"network: {type(exc).__name__}: {redact_error_text(str(exc))}")


async def impersonated_get(
    url: str,
    *,
    timeout_s: float,
    wall_timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    proxy: str | None = None,
    profile: str = "chrome",
) -> Reply:
    """GET ``url``; transport faults come back as ``err`` (``timeout:``/``network:``), never raised.

    The body is streamed so the cap stops the read instead of judging a buffered whole.
    """
    return await _send(
        curl_requests.get,
        url,
        timeout_s=timeout_s,
        wall_timeout_s=wall_timeout_s,
        max_bytes=max_bytes,
        profile=profile,
        headers=headers,
        cookies=cookies,
        proxy=proxy,
    )


async def impersonated_post(
    url: str,
    body: bytes,
    *,
    timeout_s: float,
    wall_timeout_s: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    proxy: str | None = None,
    profile: str = "chrome",
) -> Reply:
    """POST ``body`` to ``url``; same contract as ``impersonated_get``."""
    return await _send(
        curl_requests.post,
        url,
        timeout_s=timeout_s,
        wall_timeout_s=wall_timeout_s,
        max_bytes=max_bytes,
        profile=profile,
        data=body,
        headers=headers,
        cookies=cookies,
        proxy=proxy,
    )


async def shop_request(
    url: str,
    *,
    pacer: Pacer,
    provider: str,
    label: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout_s: float = 20.0,
    max_bytes: int = 8_000_000,
    retries: int = 1,
    backoff_s: float = 2.0,
    proxy: str | None = None,
) -> Reply:
    """One request to a shop by the plugin's rules; GET, or POST when ``body`` is given.

    Waits its turn and raises ``SourceStoppedError`` for a stopped shop. Only a
    network fault or a 502/503/504 is retried. A 401/403/418/429 stops the shop
    and raises; any other status comes back for the caller to judge.
    """
    attempt = 0
    while True:
        await pacer.wait()
        options: dict[str, Any] = {
            "timeout_s": timeout_s,
            "wall_timeout_s": timeout_s + 5,
            "max_bytes": max_bytes,
            "headers": headers,
            "cookies": cookies,
            "proxy": proxy,
        }
        reply = await (impersonated_get(url, **options) if body is None else impersonated_post(url, body, **options))
        if (reply.err is not None and reply.status == 0) or reply.status in TRANSIENT_STATUSES:
            if attempt < retries:
                attempt += 1
                await asyncio.sleep(backoff_s * attempt)
                continue
            raise TransportDownError(
                f"{label}: {reply.err or f'HTTP {reply.status}'}", provider=provider, status_code=reply.status or None
            )
        if reply.status in REFUSAL_STATUSES:
            pacer.record_refusal(f"http {reply.status}")
            raise TransportDownError(
                f"{label}: {provider} refused the request (HTTP {reply.status}); "
                f"{provider} is stopped for 30 min, do not retry",
                provider=provider,
                status_code=reply.status,
            )
        if reply.err is not None:
            raise TransportDownError(f"{label}: {reply.err}", provider=provider, status_code=reply.status or None)
        return reply
