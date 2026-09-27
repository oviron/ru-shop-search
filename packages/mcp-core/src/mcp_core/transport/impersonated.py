"""Tier 1 for shops that check the TLS fingerprint: one GET through curl_cffi as Chrome.

No retries and no redirects: the caller sees every status and ``Location`` and
decides whether it was a refusal. Needs the ``mcp-core[impersonate]`` extra.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from curl_cffi import requests as curl_requests

from mcp_core.redact import redact_error_text


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

    def _fetch() -> Reply:
        resp = curl_requests.get(
            url,
            headers=headers,
            cookies=cookies,
            impersonate=profile,
            timeout=timeout_s,
            allow_redirects=False,
            proxy=proxy,
            stream=True,
        )
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
