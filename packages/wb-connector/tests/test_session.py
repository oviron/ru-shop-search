"""Токен поиска WB: кэш в данных плагина и выпуск во вкладке профиля."""

from __future__ import annotations

import asyncio
import json
import stat
import time
from contextlib import asynccontextmanager

import pytest
from wb_connector import session as wb_session

TOKEN = "AbC.def-123_456+789/xyz=" * 4
UA = "Mozilla/5.0 (Macintosh) Chrome/153.0.0.0 Safari/537.36"


def _session(**overrides) -> wb_session.WbSession:
    values = {"token": TOKEN, "device_id": "site_" + "a" * 32, "user_agent": UA, "minted_at": time.time()}
    values.update(overrides)
    return wb_session.WbSession(**values)


def test_session_lives_in_plugin_data_and_only_the_owner_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    wb_session.save(_session())

    path = tmp_path / "wb_session.json"
    assert wb_session.session_path() == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert wb_session.load() == _session(minted_at=wb_session.load().minted_at)


def test_an_expired_session_is_not_used(tmp_path):
    path = tmp_path / "s.json"
    wb_session.save(_session(minted_at=time.time() - wb_session.TTL_S - 1), path)
    assert wb_session.load(path) is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"token": TOKEN}),
        json.dumps({"token": "a;b" * 10, "device_id": "site_" + "a" * 32, "user_agent": UA, "minted_at": 0}),
        json.dumps({"token": TOKEN, "device_id": "evil", "user_agent": UA, "minted_at": time.time()}),
        json.dumps(
            {"token": TOKEN, "device_id": "site_" + "a" * 32, "user_agent": "x\r\nX: y", "minted_at": time.time()}
        ),
    ],
)
def test_a_damaged_session_file_reads_as_no_session(tmp_path, content):
    path = tmp_path / "s.json"
    path.write_text(content)
    assert wb_session.load(path) is None


def test_clear_removes_the_session(tmp_path):
    path = tmp_path / "s.json"
    wb_session.save(_session(), path)
    wb_session.clear(path)
    wb_session.clear(path)
    assert not path.exists()


class _Page:
    def __init__(self, cookies: list[str]):
        self._cookies = cookies
        self.polls = 0

    async def evaluate(self, js: str):
        if "userAgent" in js:
            return UA
        self.polls += 1
        return self._cookies.pop(0) if self._cookies else ""


def _fake_open_page(page: _Page, opened: list[str]):
    @asynccontextmanager
    async def fake(url, wait_ms=5000, **kwargs):
        opened.append(url)
        yield page

    return fake


@pytest.fixture
def instant_sleep(monkeypatch):
    async def no_sleep(_s):
        return None

    monkeypatch.setattr(wb_session.asyncio, "sleep", no_sleep)


def test_mint_waits_for_the_site_to_set_its_token(monkeypatch, instant_sleep):
    page = _Page(["", "", TOKEN])
    opened: list[str] = []
    monkeypatch.setattr(wb_session, "open_page", _fake_open_page(page, opened))

    minted = asyncio.run(wb_session.mint())

    assert opened == ["https://www.wildberries.ru/"]
    assert page.polls == 3
    assert minted.token == TOKEN
    assert minted.user_agent == UA
    assert minted.device_id.startswith("site_") and len(minted.device_id) == 37
    assert minted.is_fresh()


def test_no_token_in_time_is_reported_not_retried(monkeypatch, instant_sleep):
    page = _Page([])
    opened: list[str] = []
    monkeypatch.setattr(wb_session, "open_page", _fake_open_page(page, opened))

    with pytest.raises(wb_session.TokenNotIssued):
        asyncio.run(wb_session.mint(timeout_s=0))
    assert opened == ["https://www.wildberries.ru/"]
