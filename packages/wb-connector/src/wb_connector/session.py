"""WB search session: the ``x_wbaas_token`` cookie minted by the plugin's Chrome profile.

The site sets the cookie after its own first request draws a 498; it lives 36 h and is
cached in ``${CLAUDE_PLUGIN_DATA}/wb_session.json`` (mode 600), never in git or logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from mcp_core.plugin_data import plugin_data_dir
from mcp_core.transport.chrome_cdp import open_page

HOME_URL = "https://www.wildberries.ru/"
TTL_S = 30 * 3600
MINT_TIMEOUT_S = 15.0
_FILE = "wb_session.json"
_TOKEN_RE = re.compile(r'^[^\s;,"\\]{20,8192}$')
_DEVICE_RE = re.compile(r"^site_[0-9a-f]{32}$")
_TOKEN_JS = "() => (document.cookie.split('; ').find(c => c.startsWith('x_wbaas_token=')) || '').slice(14)"
_UA_JS = "() => navigator.userAgent"


class TokenNotIssued(RuntimeError):
    """The tab loaded but WB set no token: a captcha or a wall stands in front of it."""


@dataclass(frozen=True)
class WbSession:
    token: str
    device_id: str
    user_agent: str
    minted_at: float

    def is_fresh(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.minted_at < TTL_S


def session_path() -> Path:
    return plugin_data_dir() / _FILE


def load(path: Path | None = None) -> WbSession | None:
    """The cached session, or None when missing, malformed or expired."""
    try:
        raw = json.loads((path or session_path()).read_text(encoding="utf-8"))
        session = WbSession(
            token=str(raw["token"]),
            device_id=str(raw["device_id"]),
            user_agent=str(raw["user_agent"]),
            minted_at=float(raw["minted_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (_TOKEN_RE.match(session.token) and _DEVICE_RE.match(session.device_id)):
        return None
    if "\n" in session.user_agent or "\r" in session.user_agent:
        return None
    return session if session.is_fresh() else None


def save(session: WbSession, path: Path | None = None) -> None:
    target = path or session_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(session), fh)
    os.replace(tmp, target)


def clear(path: Path | None = None) -> None:
    (path or session_path()).unlink(missing_ok=True)


async def mint(timeout_s: float = MINT_TIMEOUT_S) -> WbSession:
    """Open the WB home page in the profile and wait for the site to set its token.

    Raises ``NavBlocked`` when the page itself is refused and ``TokenNotIssued``
    when no token appears within ``timeout_s``.
    """
    async with open_page(HOME_URL, wait_ms=0) as page:
        deadline = time.monotonic() + timeout_s
        while True:
            token = await page.evaluate(_TOKEN_JS)
            if isinstance(token, str) and _TOKEN_RE.match(token):
                break
            if time.monotonic() >= deadline:
                raise TokenNotIssued(f"WB set no x_wbaas_token within {timeout_s:.0f}s")
            await asyncio.sleep(0.5)
        user_agent = await page.evaluate(_UA_JS)
    if not isinstance(user_agent, str) or not user_agent or "\n" in user_agent or "\r" in user_agent:
        raise TokenNotIssued("the tab reported no usable user agent")
    return WbSession(
        token=token,
        device_id=f"site_{uuid.uuid4().hex}",
        user_agent=user_agent,
        minted_at=time.time(),
    )
